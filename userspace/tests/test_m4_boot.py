#!/usr/bin/python3 -I
"""m4-boot tests: Boot ABI v1 writer, entry rotation, refusals and the boot counter (boot-pending / boot-ok / safe mode)."""
import contextlib
import hashlib
import io
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = os.environ.get('M4_BOOT', str(Path(__file__).resolve().parents[1]/'support/usr/bin/m4-boot'))
loader = importlib.machinery.SourceFileLoader('m4boot', SOURCE)
spec = importlib.util.spec_from_loader('m4boot', loader)
m4boot = importlib.util.module_from_spec(spec)
loader.exec_module(m4boot)

import struct


def make_image(size=8192, magic=b'ARMd', text_offset=0, flags=4):
    """A raw arm64 Image header (text_offset, image_size, flags) padded to `size` bytes."""
    raw = bytearray(size)
    struct.pack_into('<QQQ', raw, 8, text_offset, size, flags)
    raw[56:60] = magic
    return bytes(raw)


IMAGE = make_image()


class Tree:
    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.boot = root/'boot'
        self.modules = root/'modules'
        (self.boot/'m4').mkdir(parents=True)
        # the writer must not read the build host's /usr/share/m4/abi.conf: pin the ABI windows for the test
        m4boot.ABI_CONF = str(root/'abi.conf')
        m4boot.IMAGE_MAX, m4boot.INITRAMFS_MAX, m4boot.DTB_MAX = 29949952, 16*1024*1024, 36864
        self.chosen = root/'chosen'
        self.chosen.mkdir()
        self.proc_cmdline = root/'cmdline'
        self.proc_cmdline.write_text('root=UUID=x rw quiet\n')
        self.cmdline = root/'m4-cmdline'
        self.cmdline.write_text('rw quiet\n')             # the package's form: m4-boot adds the root
        self.root_uuid = '00000000-0000-0000-0000-000000000000'
        self.mkinitcpio = root/'mkinitcpio'
        self.mkinitcpio.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -g ] && out=$2; shift; done\nprintf "initramfs" > "$out"\n')
        self.mkinitcpio.chmod(0o755)
        # a depmod that records its arguments and writes the modules.dep it was asked for (the module tree is a plain directory here)
        self.depmod = root/'depmod'
        self.depmod_log = root/'depmod.log'
        self.depmod.write_text('#!/bin/sh\necho "$*" >> %s\nfor a; do k=$a; done\ntouch %s/$k/modules.dep\n' % (self.depmod_log, self.modules))
        self.depmod.chmod(0o755)
        self.systemctl = root/'systemctl'
        self.state = root/'unit-state'
        self.state.write_text('ActiveState=inactive\nActiveEnterTimestampMonotonic=0\n')
        self.systemctl.write_text('#!/bin/sh\ncat %s\n' % self.state)
        self.systemctl.chmod(0o755)
        self.gpu_ready = root/'gpu-ready'          # absent: a boot without the GPU
        self.rescue_file = root/'rescue-kver'      # absent unless a test writes it
        self.kept = root/'kept-trees'              # never the build host's /var/lib/m4/kept-trees
        self.klog = root/'kernel.log'
        self.klog.write_text('')
        self.sddm_conf = root/'sddm.conf'          # never the build host's SDDM configuration
        self.sddm_conf_dir = root/'sddm.conf.d'
        self.sddm_conf_dir.mkdir()
        self.lines = []
        self.now = 1000.0

    def dir_path(self):
        return Path(self.dir.name)

    def kernel(self, kver, image=IMAGE):
        d = self.modules/kver
        (d/'dtb/apple').mkdir(parents=True, exist_ok=True)
        (d/'modules.dep').write_text('')
        (d/'vmlinuz').write_bytes(image)
        (d/'dtb/apple/t6040-j616s.dtb').write_bytes(b'dtb' * 100)
        os.utime(d/'vmlinuz', (self.now, self.now))
        self.now += 10

    def boot_obj(self, **kw):
        return m4boot.Boot(str(self.boot), str(self.modules), str(self.cmdline), str(self.mkinitcpio), self.lines.append,
                           chosen=str(self.chosen), proc_cmdline=str(self.proc_cmdline), systemctl=str(self.systemctl),
                           sleep=self.sleep, clock=self.clock, depmod=str(self.depmod),
                           gpu_ready=str(self.gpu_ready), kernel_log=str(self.klog), sddm_uid=960, rescue_file=str(self.rescue_file),
                           kept_file=str(self.kept), root_uuid=self.root_uuid, sddm_conf=str(self.sddm_conf),
                           sddm_conf_dir=str(self.sddm_conf_dir), **kw)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def prop(self, name, value):
        (self.chosen/name).write_bytes(value.encode() + b'\0')

    def run(self, *argv):
        return m4boot.main(['--boot', str(self.boot), '--modules', str(self.modules), '--cmdline', str(self.cmdline),
                            '--mkinitcpio', str(self.mkinitcpio), '--chosen', str(self.chosen),
                            '--proc-cmdline', str(self.proc_cmdline), '--systemctl', str(self.systemctl),
                            '--depmod', str(self.depmod), '--gpu-ready', str(self.gpu_ready), '--kernel-log', str(self.klog),
                            '--sddm-uid', '960', '--rescue-kver-file', str(self.rescue_file), '--kept-file', str(self.kept),
                            '--root-uuid', self.root_uuid, '--sddm-conf', str(self.sddm_conf),
                            '--sddm-conf-dir', str(self.sddm_conf_dir), *argv],
                           out=self.lines.append, clock=self.clock, sleep=self.sleep)


class Writer(unittest.TestCase):
    def setUp(self):
        self.t = Tree(self)
        self.t.kernel('7.1.13-m4')

    def test_install_writes_a_verified_entry_and_loader_conf(self):
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        e = self.t.boot/'m4/entries/7.1.13-m4'
        self.assertEqual(sorted(p.name for p in e.iterdir()), ['Image', 'board.dtb', 'entry.conf', 'initramfs.img'])
        conf = m4boot.parse_conf((e/'entry.conf').read_text())
        self.assertEqual(conf['abi'], '1')
        self.assertIn('root=UUID=', conf['cmdline'])
        self.assertEqual(conf['Image_sha256'], hashlib.sha256((e/'Image').read_bytes()).hexdigest())
        self.assertEqual(m4boot.parse_conf((self.t.boot/'m4/loader.conf').read_text()), {'abi': '1', 'default': '7.1.13-m4'})
        self.assertEqual(list((self.t.boot/'m4/entries').glob('.tmp-*')), [])

    def test_a_second_install_of_a_kver_rebuilds_its_entry_and_never_mints_a_suffix(self):
        for _ in range(3):
            self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        names = sorted(p.name for p in (self.t.boot/'m4/entries').iterdir())
        self.assertEqual(names, ['7.1.13-m4'])
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4')
        self.assertEqual(self.t.boot_obj().verify_entry('7.1.13-m4'), [])

    def test_select_rollback_and_unknown_entry(self):
        self.t.run('install', '--kver', '7.1.13-m4')
        self.t.kernel('7.1.16-m4')
        self.t.run('install', '--kver', '7.1.16-m4')
        self.assertEqual(self.t.boot_obj().default(), '7.1.16-m4')
        self.assertEqual(self.t.run('rollback'), 0)
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4')
        self.assertEqual(self.t.run('select', 'nope'), 1)
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4')

    def test_oversize_image_is_refused_and_nothing_changes(self):
        self.t.run('install', '--kver', '7.1.13-m4')
        before = (self.t.boot/'m4/loader.conf').read_bytes()
        self.t.kernel('7.1.14-m4', make_image(m4boot.IMAGE_MAX + 4096))
        self.assertEqual(self.t.run('install', '--kver', '7.1.14-m4'), 1)
        self.assertEqual((self.t.boot/'m4/loader.conf').read_bytes(), before)
        self.assertFalse((self.t.boot/'m4/entries/7.1.14-m4').exists())

    def entry_cmdline(self):
        return m4boot.parse_conf((self.t.boot/'m4/entries/7.1.13-m4/entry.conf').read_text())['cmdline']

    def test_a_command_line_without_root_gets_the_root_m4_boot_runs_on(self):
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.entry_cmdline(), 'root=UUID=%s rw quiet' % self.t.root_uuid)

    def test_a_command_line_with_root_is_used_verbatim(self):
        self.t.cmdline.write_text('root=PARTUUID=abc rw  quiet\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.entry_cmdline(), 'root=PARTUUID=abc rw quiet')

    def test_a_kernel_drops_the_words_its_package_lists(self):
        self.t.cmdline.write_text('root=UUID=x rw idle=nop quiet\n')
        (self.t.modules/'7.1.13-m4/cmdline-drop').write_text('idle=nop\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.entry_cmdline(), 'root=UUID=x rw quiet')

    def test_a_kernel_without_a_drop_list_gets_the_whole_command_line(self):
        self.t.cmdline.write_text('root=UUID=x rw idle=nop quiet\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.entry_cmdline(), 'root=UUID=x rw idle=nop quiet')

    def test_bad_command_line_is_refused(self):
        self.t.cmdline.write_text('root=UUID=x ' + 'a' * 1100 + '\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 1)
        self.assertFalse((self.t.boot/'m4/loader.conf').exists())
        self.t.cmdline.write_text('quiet \x07\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 1)

    def test_a_corrupted_entry_is_not_selectable(self):
        self.t.run('install', '--kver', '7.1.13-m4')
        self.t.kernel('7.1.16-m4')
        self.t.run('install', '--kver', '7.1.16-m4')
        (self.t.boot/'m4/entries/7.1.13-m4/Image').write_bytes(b'x')
        self.assertEqual(self.t.run('select', '7.1.13-m4'), 1)
        self.assertEqual(self.t.run('status'), 0)

    def test_gzip_vmlinuz_is_decompressed_to_a_raw_image(self):
        import gzip
        self.t.kernel('7.1.15-m4', gzip.compress(IMAGE))
        self.assertEqual(self.t.run('install', '--kver', '7.1.15-m4'), 0)
        self.assertEqual((self.t.boot/'m4/entries/7.1.15-m4/Image').read_bytes(), IMAGE)

    def test_entry_conf_carries_size_crc32_and_sha256_for_every_file(self):
        self.t.run('install', '--kver', '7.1.13-m4')
        conf = m4boot.parse_conf((self.t.boot/'m4/entries/7.1.13-m4/entry.conf').read_text())
        import zlib
        for f in ('Image', 'initramfs.img', 'board.dtb'):
            data = (self.t.boot/'m4/entries/7.1.13-m4'/f).read_bytes()
            self.assertEqual(conf[f + '_size'], str(len(data)))
            self.assertEqual(conf[f + '_crc32'], '%08x' % (zlib.crc32(data) & 0xffffffff))
            self.assertEqual(len(conf[f + '_sha256']), 64)

    def test_image_header_the_loader_would_refuse_is_refused_here(self):
        for bad in (make_image(text_offset=0x80000), make_image(flags=0), make_image(magic=b'XXXX')):
            self.t.kernel('7.1.17-m4', bad)
            self.assertEqual(self.t.run('install', '--kver', '7.1.17-m4'), 1)
        self.assertFalse((self.t.boot/'m4/entries/7.1.17-m4').exists())

    def test_dtb_window_is_the_loaders_36864(self):
        self.t.prop('m4,loader-version', 'rev11')     # booted by a loader that publishes no window
        self.t.kernel('7.1.18-m4')
        (self.t.modules/'7.1.18-m4/dtb/apple/t6040-j616s.dtb').write_bytes(b'd' * 36865)
        self.assertEqual(self.t.run('install', '--kver', '7.1.18-m4'), 1)
        (self.t.modules/'7.1.18-m4/dtb/apple/t6040-j616s.dtb').write_bytes(b'd' * 36864)
        self.assertEqual(self.t.run('install', '--kver', '7.1.18-m4'), 0)

    def test_entry_names_follow_the_abi(self):
        for name, ok in (('7.1.13-m4', True), ('7.1.13+x', True), ('.hidden', False), ('a..b', False), ('x' * 64, False), ('x' * 63, True)):
            self.assertEqual(m4boot.valid_name(name), ok, name)

    def test_no_default_install_keeps_the_default(self):
        self.t.run('install', '--kver', '7.1.13-m4')
        self.t.kernel('7.1.16-m4')
        self.assertEqual(self.t.run('install', '--kver', '7.1.16-m4', '--no-default'), 0)
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4')


class RootUuid(unittest.TestCase):
    def test_the_package_command_line_names_no_root(self):
        words = (Path(SOURCE).parents[1]/'share/m4/cmdline').read_text().split()
        self.assertFalse([w for w in words if w.startswith('root=') or '@M4_' in w])

    @unittest.skipUnless(shutil.which('findmnt'), 'needs findmnt')
    def test_the_kernel_names_the_same_root_as_findmnt(self):
        import subprocess
        want = subprocess.run(['findmnt', '-n', '-o', 'UUID', '/'], capture_output=True, text=True).stdout.strip()
        if not want:
            self.skipTest('/ has no file system UUID here')
        self.assertEqual(m4boot.fs_uuid('/'), want)

    def test_a_path_that_is_not_a_directory_is_refused(self):
        with self.assertRaises(m4boot.Refused):
            m4boot.fs_uuid(SOURCE)


class InPlace(unittest.TestCase):
    """N3/H-1: every m4-support upgrade runs `m4-boot install` for the unchanged kernel; the entry is rebuilt under its own name and the rollback stays."""
    def setUp(self):
        self.t = Tree(self)
        self.t.kernel('7.1.13-m4')
        self.t.kernel('7.1.13-m4.4')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)       # the N1 entry
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4.4'), 0)     # the N2 entry: default
        self.entries = self.t.boot/'m4/entries'

    def names(self):
        return sorted(p.name for p in self.entries.iterdir())

    def check_n2_state(self):
        b = self.t.boot_obj()
        self.assertEqual(self.names(), ['7.1.13-m4', '7.1.13-m4.4'])
        self.assertEqual(b.default(), '7.1.13-m4.4')
        for n in self.names():
            self.assertEqual(b.verify_entry(n), [], n)
        self.assertEqual(b.previous(), '7.1.13-m4')

    def test_the_n2_state_is_as_the_chain_expects(self):
        self.check_n2_state()

    def test_an_m4_support_reinstall_keeps_both_entries_and_the_default(self):
        for _ in range(3):
            self.assertEqual(self.t.run('install'), 0)           # what 90-m4-boot.hook runs: newest kernel, no arguments
            self.check_n2_state()
        self.assertIn('rebuilt in place', '\n'.join(self.t.lines))
        self.assertEqual(list(self.entries.glob('.tmp-*')) + list(self.entries.glob('.old-*')), [])

    def test_the_rebuild_replaces_the_files_of_the_entry(self):
        before = (self.entries/'7.1.13-m4.4/initramfs.img').read_bytes()
        self.t.mkinitcpio.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -g ] && out=$2; shift; done\nprintf "a newer initramfs" > "$out"\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4.4'), 0)
        after = (self.entries/'7.1.13-m4.4/initramfs.img').read_bytes()
        self.assertNotEqual(before, after)
        self.assertEqual(after, b'a newer initramfs')
        self.check_n2_state()

    def test_without_the_atomic_exchange_the_entry_is_renamed_aside_and_the_result_is_the_same(self):
        real = m4boot.exchange_dirs
        m4boot.exchange_dirs = lambda a, b: False
        try:
            self.assertEqual(self.t.run('install'), 0)
        finally:
            m4boot.exchange_dirs = real
        self.check_n2_state()
        self.assertEqual(list(self.entries.glob('.old-*')), [])

    def test_a_duplicate_of_the_bad_state_collapses_and_the_rollback_stays(self):
        # the state the VM chain produced: 7.1.13-m4.4 and 7.1.13-m4.4.2 (same kver, default), no 7.1.13-m4
        shutil.copytree(self.entries/'7.1.13-m4.4', self.entries/'7.1.13-m4.4.2')
        b = self.t.boot_obj()
        b.set_default('7.1.13-m4.4.2')
        self.assertEqual(self.t.run('install'), 0)
        self.assertEqual(self.names(), ['7.1.13-m4', '7.1.13-m4.4'])
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4.4')

    def test_prune_keeps_the_only_entry_of_the_other_kver(self):
        self.assertEqual(self.t.run('prune'), 0)
        self.check_n2_state()

    def test_prune_keeps_the_rescue_kvers_entry_even_with_two_newer_kernels(self):
        self.t.kernel('7.1.13-m4.5')
        self.t.kernel('7.1.13-m4.6')
        self.t.rescue_file.write_text('7.1.13-m4\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4.5'), 0)
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4.6'), 0)
        self.assertEqual(self.names(), ['7.1.13-m4', '7.1.13-m4.5', '7.1.13-m4.6'])
        self.assertEqual(self.t.boot_obj().default(), '7.1.13-m4.6')

    def test_a_rebuild_that_does_not_verify_leaves_the_old_entry_untouched(self):
        before = {p.name: p.read_bytes() for p in (self.entries/'7.1.13-m4.4').iterdir()}
        self.t.mkinitcpio.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -g ] && out=$2; shift; done\n: > "$out"\n')   # empty initramfs
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4.4'), 1)
        after = {p.name: p.read_bytes() for p in (self.entries/'7.1.13-m4.4').iterdir()}
        self.assertEqual(before, after)
        self.check_n2_state()

    def test_an_aside_copy_left_by_a_cut_rebuild_is_restored(self):
        os.rename(self.entries/'7.1.13-m4.4', self.entries/'.old-7.1.13-m4.4')      # cut between the two renames
        (self.entries/'.tmp-7.1.13-m4.4').mkdir()
        self.assertEqual(self.t.run('prune'), 0)
        self.check_n2_state()


class Interrupted(unittest.TestCase):
    """N1-VM-A/6: what a killed package transaction or a killed entry write leaves behind."""
    def setUp(self):
        self.t = Tree(self)
        self.t.kernel('7.1.13-m4')

    def depmod_calls(self):
        return self.t.depmod_log.read_text().splitlines() if self.t.depmod_log.exists() else []

    def test_a_missing_modules_dep_is_generated_before_the_initramfs_is_built(self):
        (self.t.modules/'7.1.13-m4/modules.dep').unlink()
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0, self.t.lines)
        self.assertEqual(self.depmod_calls(), ['-a 7.1.13-m4'])
        self.assertTrue((self.t.modules/'7.1.13-m4/modules.dep').exists())
        self.assertTrue((self.t.boot/'m4/entries/7.1.13-m4').is_dir())

    def test_modules_dep_older_than_a_module_is_generated_again(self):
        mod = self.t.modules/'7.1.13-m4/kernel/drivers/nvme/host/nvme-apple.ko.zst'
        mod.parent.mkdir(parents=True)
        mod.write_bytes(b'ko')
        dep = self.t.modules/'7.1.13-m4/modules.dep'
        os.utime(dep, (100, 100))
        os.utime(mod, (200, 200))
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.depmod_calls(), ['-a 7.1.13-m4'])

    def test_a_current_modules_dep_is_left_alone(self):
        mod = self.t.modules/'7.1.13-m4/kernel/x.ko'
        mod.parent.mkdir(parents=True)
        mod.write_bytes(b'ko')
        os.utime(mod, (100, 100))
        os.utime(self.t.modules/'7.1.13-m4/modules.dep', (200, 200))
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(self.depmod_calls(), [])

    def test_a_failing_depmod_refuses_and_writes_nothing(self):
        (self.t.modules/'7.1.13-m4/modules.dep').unlink()
        self.t.depmod.write_text('#!/bin/sh\necho boom >&2\nexit 1\n')
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 1)
        self.assertFalse((self.t.boot/'m4/loader.conf').exists())

    def test_install_sweeps_work_directories_and_entry_temporaries_of_a_killed_run(self):
        m4 = self.t.boot/'m4'
        stale = [m4/'m4-boot-ab12_x9z', m4/'m4-boot-zzzzzzzz']
        for d in stale:
            d.mkdir()
            (d/'initramfs.img').write_bytes(b'x' * 1000)
        (m4/'entries/.tmp-7.1.13-m4').mkdir(parents=True)
        (m4/'entries/.tmp-7.1.13-m4/Image').write_bytes(b'x')
        mine = m4/'m4-boot-notours-longer-name'       # not a tempfile name of ours: left alone
        mine.mkdir()
        (m4/'keep-me').mkdir()
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertFalse(any(d.exists() for d in stale))
        self.assertFalse((m4/'entries/.tmp-7.1.13-m4').exists())
        self.assertTrue(mine.is_dir() and (m4/'keep-me').is_dir())
        self.assertIn('removed 3 leftover', '\n'.join(self.t.lines))
        self.assertEqual(sorted(p.name for p in m4.glob('m4-boot-*')), ['m4-boot-notours-longer-name'])

    def test_a_clean_install_leaves_no_work_directory(self):
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        self.assertEqual(list((self.t.boot/'m4').glob('m4-boot-*')), [])

    def test_prune_sweeps_too(self):
        self.assertEqual(self.t.run('install', '--kver', '7.1.13-m4'), 0)
        d = self.t.boot/'m4/m4-boot-qwertyui'
        d.mkdir()
        self.assertEqual(self.t.run('prune'), 0)
        self.assertFalse(d.exists())


ADOPTED_960 = 'asahi_m4: G16: render-reuse adopted slot=0 uid=960 file=5 vm=3 queue=1 cold=1'
ADOPTED_1000 = 'asahi_m4: G16: render-reuse adopted slot=1 uid=1000 file=9 vm=4 queue=2 cold=1'
REJECTED_LINE = 'asahi_m4: T6040 submit rejected command shape: renders=1 computes=0 attachments=1'
NOISE = 'asahi_m4: G16: render-reuse pause-enter file=5 vm=3 queue=1 backend=Some(0) elapsed_ms=2'


class GreeterGate(unittest.TestCase):
    """N3/C: with the GPU up the mark is cleared only when the greeter holds a GPU slot and nothing was rejected."""
    def setUp(self):
        self.t = Tree(self)
        self.t.kernel('7.1.13-m4')
        self.t.run('install', '--kver', '7.1.13-m4')
        self.pending = self.t.boot/'m4/boot-pending'
        self.pending.write_text('7.1.13-m4\n')
        self.t.state.write_text('ActiveState=active\nActiveEnterTimestampMonotonic=%d\n' % int((self.t.now - 25) * 1e6))

    def gpu(self, *lines):
        self.t.gpu_ready.write_text('ok\n')
        self.t.klog.write_text('\n'.join(('boot noise',) + lines) + '\n')

    def test_the_greeters_adoption_line_clears_the_mark(self):
        self.gpu(NOISE, ADOPTED_960)
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())
        self.assertIn('the greeter (uid 960) holds a GPU slot', '\n'.join(self.t.lines))

    def test_no_adoption_line_keeps_the_mark_after_the_grace(self):
        self.gpu(NOISE)
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())
        self.assertIn('the greeter has not drawn on the GPU', '\n'.join(self.t.lines))

    def test_another_clients_adoption_is_not_the_greeters(self):
        self.gpu(ADOPTED_1000, 'asahi_m4: G16: render-reuse adopted slot=2 uid=9600 file=1 vm=1 queue=1 cold=0')
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())

    def autologin(self, user):
        (self.t.sddm_conf_dir/'zzz-autologin.conf').write_text('[Autologin]\nUser=%s\nSession=m4-hyprland.desktop\n' % user)

    def test_autologin_users_adoption_clears_the_mark(self):
        import pwd
        me = pwd.getpwuid(os.getuid())
        self.autologin(me.pw_name)
        self.gpu(NOISE, 'asahi_m4: G16: render-reuse adopted slot=1 uid=%d file=9 vm=4 queue=2 cold=1' % me.pw_uid)
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())
        self.assertIn('autologin: %s (uid %d) holds a GPU slot' % (me.pw_name, me.pw_uid), '\n'.join(self.t.lines))

    def test_another_clients_adoption_still_fails_without_autologin(self):
        import pwd
        me = pwd.getpwuid(os.getuid())
        self.gpu(NOISE, 'asahi_m4: G16: render-reuse adopted slot=1 uid=%d file=9 vm=4 queue=2 cold=1' % me.pw_uid)
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())

    def test_aurora_mode_without_failed_work_clears_the_mark(self):
        self.t.gpu_ready.write_text('ok aurora\n')
        self.t.klog.write_text('boot noise\n[drm] Initialized asahi 0.0.0 for asahi-t6040 on minor 1\n')
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())
        self.assertIn('aurora GPU driver up, no failed GPU work', '\n'.join(self.t.lines))

    def test_aurora_mode_with_failed_work_keeps_the_mark(self):
        self.t.gpu_ready.write_text('ok aurora\n')
        self.t.klog.write_text('asahi: GPU work of a client failed (Error(5), reported at g17/runtime.rs:803)\n')
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())
        self.assertIn('the aurora GPU driver failed a client', '\n'.join(self.t.lines))

    def test_a_rejected_submit_keeps_the_mark_even_with_the_adoption(self):
        self.gpu(ADOPTED_960, REJECTED_LINE)
        before = self.t.now
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.t.now, before, 'refused at once, without waiting')
        self.assertIn('the GPU driver rejected a submit', '\n'.join(self.t.lines))

    def test_the_black_n2_boot_is_not_ok(self):
        # the journal of the owner's black boots: a rejected shape and no adoption at all
        self.gpu('asahi_m4: DRM_IOCTL_ASAHI_QUEUE_CREATE failed: Invalid argument', REJECTED_LINE)
        self.assertEqual(self.t.run('pending', 'ok'), 1)
        self.assertTrue(self.pending.exists())

    def test_an_adoption_a_moment_later_is_accepted(self):
        self.gpu(NOISE)
        count = []
        real = self.t.sleep

        def sleep(seconds):
            real(seconds)
            count.append(1)
            if len(count) == 5:
                self.t.klog.write_text(self.t.klog.read_text() + ADOPTED_960 + '\n')
        self.t.sleep = sleep
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())

    def test_without_the_gpu_the_old_rule_stands(self):
        self.t.klog.write_text(REJECTED_LINE + '\n')          # no /run/m4/gpu-ready: not this driver's boot
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())

    def test_the_driver_still_prints_the_lines_the_gate_looks_for(self):
        for tree in ('/work/n2/kernel/linux-m4', '/work/n1/kernel/linux-m4'):
            reuse = Path(tree)/'drivers/gpu/drm/asahi/g16_reuse.rs'
            drm = Path(tree)/'drivers/gpu/drm/asahi/g16_drm.rs'
            if reuse.is_file() and drm.is_file():
                break
        else:
            self.skipTest('no kernel tree here')
        self.assertIn('pr_info!("G16: render-reuse adopted slot={} uid={} ', reuse.read_text())
        self.assertIn('pr_err!("T6040 submit rejected command shape', drm.read_text())
        # and the gate's patterns match the formatted lines of exactly those format strings
        self.assertEqual(m4boot.ADOPTED, 'render-reuse adopted')
        self.assertEqual(m4boot.REJECTED, 'submit rejected')


class LoaderDtbWindow(unittest.TestCase):
    """board.dtb: the limit the running loader publishes (/chosen/m4,board-dtb-max, rev 12: 98,304), else rev 11's 36,864,
    or 98,304 when no loader of ours booted the system (a chroot); an abi.conf dtb_max line overrides all.
    Hermetic: limits() reads only the paths it is given."""
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.chosen = Path(self.dir.name)/'chosen'
        self.chosen.mkdir()
        self.conf = Path(self.dir.name)/'abi.conf'
        saved = (m4boot.IMAGE_MAX, m4boot.INITRAMFS_MAX, m4boot.DTB_MAX)
        self.addCleanup(lambda: setattr(m4boot, 'DTB_MAX', saved[2]))

    def publish(self, raw):
        (self.chosen/'m4,board-dtb-max').write_bytes(raw)

    def booted_by_loader(self):
        (self.chosen/'m4,loader-version').write_bytes(b'rev11\0')

    def limit(self):
        m4boot.limits(conf=str(self.conf), chosen=str(self.chosen))
        return m4boot.DTB_MAX

    def test_rev12_publishes_98304(self):
        self.publish((98304).to_bytes(4, 'big'))
        self.assertEqual(self.limit(), 98304)

    def test_rev11_publishes_nothing(self):
        self.booted_by_loader()
        m4boot.DTB_MAX = 98304      # a value left over from an earlier call is not kept
        self.assertEqual(self.limit(), 36864)

    def test_a_chroot_without_our_loader_gets_the_rev12_window(self):
        self.assertEqual(self.limit(), 98304)
        self.chosen.rmdir()         # no /proc/device-tree/chosen at all
        self.assertEqual(self.limit(), 98304)

    def test_implausible_values_fall_back_to_rev11(self):
        self.booted_by_loader()
        for raw in (b'', b'\x00\x01\x80', (0).to_bytes(4, 'big'), (131073).to_bytes(4, 'big'), (98304).to_bytes(8, 'big')):
            self.publish(raw)
            self.assertEqual(self.limit(), 36864, raw)

    def test_a_smaller_published_limit_is_respected(self):
        self.publish((30000).to_bytes(4, 'big'))
        self.assertEqual(self.limit(), 30000)

    def test_abi_conf_dtb_max_overrides(self):
        self.publish((98304).to_bytes(4, 'big'))
        self.conf.write_text('dtb_max=36864\n')
        self.assertEqual(self.limit(), 36864)

    def test_the_shipped_abi_conf_does_not_pin_the_rev11_limit(self):
        shipped = Path(SOURCE).resolve().parents[1]/'share/m4/abi.conf'
        values = m4boot.parse_conf(shipped.read_text())
        self.assertNotIn('dtb_max', values)
        self.assertEqual((values['image_max'], values['initramfs_max']), ('29949952', '16777216'))


class Counter(unittest.TestCase):
    def setUp(self):
        self.t = Tree(self)
        self.t.kernel('7.1.13-m4')
        self.t.run('install', '--kver', '7.1.13-m4')
        self.pending = self.t.boot/'m4/boot-pending'

    def active(self, since):
        self.t.state.write_text('ActiveState=active\nActiveEnterTimestampMonotonic=%d\n' % int(since * 1e6))

    def test_pending_set_writes_the_boot_entry_durably(self):
        self.t.prop('m4,boot-entry', '7.1.13-m4')
        self.assertEqual(self.t.run('pending', 'set'), 0)
        self.assertEqual(self.pending.read_text(), '7.1.13-m4\n')
        self.assertEqual(list((self.t.boot/'m4').glob('.*.tmp')), [])

    def test_pending_set_falls_back_to_the_default_entry(self):
        self.assertEqual(self.t.run('pending', 'set'), 0)
        self.assertEqual(self.pending.read_text(), '7.1.13-m4\n')

    def test_safe_mode_touches_nothing(self):
        self.t.proc_cmdline.write_text('root=UUID=x rw m4.safe=1\n')
        self.assertEqual(self.t.run('pending', 'set'), 0)
        self.assertFalse(self.pending.exists())
        self.pending.write_text('7.1.13-m4\n')
        self.active(self.t.now - 600)
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertTrue(self.pending.exists(), 'a rescue boot must not clear the mark')

    def test_ok_removes_the_mark_after_20_seconds_of_display_manager(self):
        self.pending.write_text('7.1.13-m4\n')
        self.active(self.t.now - 25)
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertFalse(self.pending.exists())

    def test_ok_waits_until_the_display_manager_has_been_up_20_seconds(self):
        self.pending.write_text('7.1.13-m4\n')
        self.active(self.t.now - 5)
        start = self.t.now
        self.assertEqual(self.t.run('pending', 'ok'), 0)
        self.assertGreaterEqual(self.t.now - start, 15)
        self.assertFalse(self.pending.exists())

    def test_ok_times_out_and_keeps_the_mark_when_the_display_manager_is_down(self):
        self.pending.write_text('7.1.13-m4\n')
        self.assertEqual(self.t.run('pending', 'ok', '--timeout', '30'), 1)
        self.assertTrue(self.pending.exists())

    def test_ok_without_a_mark_is_a_no_op(self):
        self.assertEqual(self.t.run('pending', 'ok'), 0)

    def test_select_install_and_clear_remove_the_mark(self):
        for action in (('select', '7.1.13-m4'), ('install', '--kver', '7.1.13-m4'), ('clear',)):
            self.pending.write_text('7.1.13-m4\n')
            self.assertEqual(self.t.run(*action), 0, action)
            self.assertFalse(self.pending.exists(), action)

    def test_status_names_the_rescue_reason_and_the_way_out(self):
        self.pending.write_text('7.1.13-m4\n')
        self.t.prop('m4,boot-source', 'rescue')
        self.t.prop('m4,boot-reason', 'previous-boot-incomplete')
        self.t.proc_cmdline.write_text('root=UUID=x rw m4.safe=1\n')
        self.assertEqual(self.t.run('status'), 1)
        text = '\n'.join(self.t.lines)
        self.assertIn('this boot: rescue (reason previous-boot-incomplete)', text)
        self.assertIn('safe mode', text)
        self.assertIn('did not finish', text)
        self.assertIn('sudo m4-boot select 7.1.13-m4', text)

    def test_status_is_clean_on_a_normal_boot(self):
        self.t.prop('m4,boot-source', 'entry')
        self.t.prop('m4,boot-entry', '7.1.13-m4')
        self.assertEqual(self.t.run('status'), 0)
        self.assertIn('the loader will boot it', '\n'.join(self.t.lines))


def make_fdt(nvme_compatible=None, node='nvme@44dcc0000'):
    """A minimal flattened device tree: the root, /chosen and, if given, a top-level NVMe node with that compatible list."""
    strings = b'compatible\0'
    def node_bytes(name, props):
        out = struct.pack('>I', 1) + name.encode() + b'\0'
        out += b'\0' * (-len(out) % 4)
        for value in props:
            out += struct.pack('>III', 3, len(value), 0) + value + b'\0' * (-len(value) % 4)
        return out
    body = node_bytes('', [b'apple,j616s\0'])
    body += node_bytes('chosen', []) + struct.pack('>I', 2)
    if nvme_compatible is not None:
        body += node_bytes(node, [b''.join(c.encode() + b'\0' for c in nvme_compatible)]) + struct.pack('>I', 2)
    body += struct.pack('>II', 2, 9)
    off_rsv = 40
    off_struct = off_rsv + 16
    off_strings = off_struct + len(body)
    total = off_strings + len(strings)
    header = struct.pack('>IIIIIIIIII', 0xd00dfeed, total, off_struct, off_strings, off_rsv, 17, 16, 0, len(strings), len(body))
    return header + bytes(16) + body + strings


class NvmeBinding(unittest.TestCase):
    """J616s: a board.dtb that drops the running NVMe binding is refused (7.3.0-fbtest.1, 2026-10-23)."""
    GUARDED = 'lab,j616-nvme-partition-25g83'

    def setUp(self):
        self.t = Tree(self)
        self.t.prop('m4,loader-version', 'rev15-j616s-9')
        live = self.t.dir_path()/'nvme@44dcc0000'
        live.mkdir()
        (live/'compatible').write_bytes(self.GUARDED.encode() + b'\0apple,t6041-nvme-ans2\0')
        os.environ.pop(m4boot.NVME_GUARD_OVERRIDE, None)

    def kernel_with(self, kver, dtb):
        self.t.kernel(kver)
        (self.t.modules/kver/'dtb/apple/t6040-j616s.dtb').write_bytes(dtb)

    def test_parser_reads_the_nvme_compatible(self):
        raw = m4boot.fdt_root_node_prop(make_fdt([self.GUARDED, 'x']), 'nvme@', 'compatible')
        self.assertEqual(raw, self.GUARDED.encode() + b'\0x\0')
        self.assertIsNone(m4boot.fdt_root_node_prop(make_fdt(None), 'nvme@', 'compatible'))
        self.assertIsNone(m4boot.fdt_root_node_prop(b'dtb' * 100, 'nvme@', 'compatible'))

    def test_same_binding_installs(self):
        self.kernel_with('7.3.0-m4.28', make_fdt([self.GUARDED, 'apple,t6041-nvme-ans2']))
        self.assertEqual(self.t.run('install', '--kver', '7.3.0-m4.28'), 0)
        self.assertTrue((self.t.boot/'m4/entries/7.3.0-m4.28').is_dir())

    def test_other_binding_is_refused_and_nothing_changes(self):
        self.kernel_with('7.1.12-m4.27', make_fdt([self.GUARDED]))
        self.assertEqual(self.t.run('install', '--kver', '7.1.12-m4.27'), 0)
        conf = (self.t.boot/'m4/loader.conf').read_bytes()
        self.kernel_with('7.3.0-fbtest.1', make_fdt(['apple,t6041-nvme-ans2']))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertNotEqual(self.t.run('install', '--kver', '7.3.0-fbtest.1'), 0)
        self.assertFalse((self.t.boot/'m4/entries/7.3.0-fbtest.1').exists())
        self.assertEqual((self.t.boot/'m4/loader.conf').read_bytes(), conf)
        self.assertIn('could not take the SSD over', err.getvalue())

    def test_missing_nvme_node_is_refused(self):
        self.kernel_with('7.3.0-x', make_fdt(None))
        self.assertNotEqual(self.t.run('install', '--kver', '7.3.0-x'), 0)
        self.assertFalse((self.t.boot/'m4/entries/7.3.0-x').exists())

    def test_override(self):
        self.kernel_with('7.3.0-y', make_fdt(['apple,t6041-nvme-ans2']))
        os.environ[m4boot.NVME_GUARD_OVERRIDE] = '1'
        self.addCleanup(os.environ.pop, m4boot.NVME_GUARD_OVERRIDE, None)
        self.assertEqual(self.t.run('install', '--kver', '7.3.0-y'), 0)

    def test_no_loader_no_check(self):
        (self.t.chosen/'m4,loader-version').unlink()
        self.kernel_with('7.3.0-z', make_fdt(['apple,t6041-nvme-ans2']))
        self.assertEqual(self.t.run('install', '--kver', '7.3.0-z'), 0)


if __name__ == '__main__':
    sys.exit(unittest.main(verbosity=1).result.wasSuccessful() is False)
