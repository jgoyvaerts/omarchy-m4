#!/usr/bin/python3 -I
"""N2 lane U fixes F2, F3, F4, F6, F7, F8 and the condition list for the VM gate.   python3 -B -I test_n2_fixes.py

F2 the unit condition spelling, with a grammar check of EVERY Condition*/Assert* line and `systemd-analyze condition` on each (an unknown
   Firmware form is false, so "skipped" looks like the expected answer on a machine without that firmware: the VM gate swaps the compatible)
F3 usb/usb-test.py (was usb/attach.py), avd, aop: the REAL Host class has every attribute the module reads (the fake hosts of the old tests set them, which hid the bug)
F4 avd: the decoder's device-tree nodes must be present and enabled (m4.17: no loader stamp); the firmware is the avd-fw package's file and pacman -Qkk is clean
F6 m4-boot status: "entry 7.1.13-m4", not "entry entry 7.1.13-m4"
F7 /etc/m4/bluetooth.conf ships in m4-support (backup=), with the headset address; the key file is never read
F8 m4-test gpu rows: m4-gpu.service did not run
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SUPPORT, TOOLS = ROOT/'support', ROOT/'tools'
TESTS = TOOLS/'usr/lib/m4/tests'
PKG = Path(os.environ.get('M4_PKG_DIR', ROOT.parent/'packages'))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def unit_files():
    found = []
    for tree in (SUPPORT, TOOLS):
        for sub in ('usr/lib/systemd', 'etc/systemd'):
            base = tree/sub
            if base.is_dir():
                found += sorted(p for p in base.rglob('*') if p.is_file() and not p.is_symlink()
                                and (p.suffix in ('.service', '.socket', '.timer', '.target', '.path', '.mount', '.conf')))
    return found


CONDITION = re.compile(r'^\s*((?:Condition|Assert)[A-Za-z]+)\s*=\s*(.*?)\s*$')
FIRMWARE_FORM = re.compile(r'^!?(device-tree|uefi|device-tree-compatible\([^()\s]+\)|smbios-field\([^()]+\))$')


def conditions():
    rows = []
    for f in unit_files():
        for number, line in enumerate(f.read_text().splitlines(), 1):
            m = CONDITION.match(line)
            if m:
                tree = SUPPORT if SUPPORT in f.parents else TOOLS
                rows.append(dict(file=str(f.relative_to(tree)), tree=tree.name, line=number, key=m.group(1), value=m.group(2)))
    return rows


class F2UnitConditions(unittest.TestCase):
    def test_no_unit_uses_the_misspelled_firmware_keyword(self):
        for f in unit_files():
            self.assertNotIn('devicetree-compatible', f.read_text(), str(f))

    def test_every_firmware_condition_is_a_known_form_and_names_the_machine(self):
        rows = [r for r in conditions() if r['key'] in ('ConditionFirmware', 'AssertFirmware')]
        self.assertGreaterEqual(len(rows), 5)
        for r in rows:
            self.assertRegex(r['value'], FIRMWARE_FORM, r)
            self.assertEqual(r['value'], 'device-tree-compatible(apple,j616s)', r)

    def test_only_known_condition_keywords_are_used(self):
        known = {'ConditionFirmware', 'ConditionKernelCommandLine', 'ConditionPathExists', 'ConditionPathExistsGlob', 'ConditionDirectoryNotEmpty',
                 'ConditionFileNotEmpty', 'ConditionVirtualization', 'ConditionACPower', 'ConditionNeedsUpdate', 'ConditionFileIsExecutable',
                 'ConditionPathIsDirectory', 'ConditionPathIsSymbolicLink', 'ConditionUser', 'ConditionHost', 'ConditionArchitecture',
                 'ConditionCapability', 'ConditionFirstBoot', 'ConditionSecurity', 'ConditionEnvironment', 'ConditionCPUs', 'ConditionMemory'}
        for r in conditions():
            self.assertIn(r['key'], known, r)

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'needs systemd-analyze')
    def test_systemd_evaluates_every_condition_line(self):
        # systemd-analyze condition prints "failed"/"succeeded" per line and complains ("Failed to ...", "Unknown") about a form it cannot read.
        for r in conditions():
            p = subprocess.run(['systemd-analyze', 'condition', '%s=%s' % (r['key'], r['value'])], capture_output=True, text=True, timeout=60)
            text = p.stdout + p.stderr
            self.assertNotRegex(text, r'(?i)failed to|unknown|invalid|not understood', (r, text))
            self.assertRegex(text, r'succeeded|failed\.', (r, text))

    def test_the_grammar_check_rejects_the_old_spelling(self):
        # on a machine WITH a device tree the old form is false and the new one true; without one both are false, so the control is the grammar
        self.assertRegex('device-tree-compatible(apple,j616s)', FIRMWARE_FORM)
        self.assertNotRegex('devicetree-compatible(apple,j616s)', FIRMWARE_FORM)

    # what the gate must see on an installed N1/N2 disk in a guest at graphical.target; everything else depends on boot state (GPU ready, AOP
    # marker, boot-pending, the loader's /chosen) and is evaluated for syntax only
    MUST_SUCCEED = {('ConditionFirmware', 'device-tree-compatible(apple,j616s)'), ('ConditionKernelCommandLine', '!m4.safe'),
                    ('ConditionVirtualization', '!container'), ('ConditionPathExists', '/etc/m4/bluetooth.conf'), ('ConditionPathExists', '/boot/m4'),
                    ('ConditionPathExists', '/etc/systemd/system/display-manager.service')}
    # parse-only on purpose: the owner's opt-outs (/etc/m4/aop-off, sensors-off, ...) are state, not a fault

    def test_the_condition_list_for_the_vm_gate_is_complete_and_publishable(self):
        rows = conditions()
        names = {Path(r['file']).name for r in rows}
        for unit in ('m4-gpu.service', 'm4-display.service', 'm4-kbd-idle.service',
                     'm4-bt-connect.service'):
            self.assertIn(unit, names)
        for r in rows:
            r['gate'] = 'must-succeed' if (r['key'], r['value']) in self.MUST_SUCCEED else 'parse-only'
        out = Path(os.environ.get('M4_CONDITIONS_OUT', tempfile.gettempdir()))/'unit-conditions.json'
        doc = dict(schema=1, lane='U', release='N2', packages=['m4-support 1.0-2', 'm4-tools 1.0-2'],
                   purpose='VM gate input (F2): every Condition*/Assert* line of every unit and drop-in the packages ship. In the guest evaluate each '
                           'line with `systemd-analyze condition KEY=VALUE` after replacing the compatible string of ConditionFirmware by the guest\'s '
                           '(linux,dummy-virt). gate=must-succeed lines must succeed on the installed disk; gate=parse-only lines depend on boot state '
                           'and only have to be understood (no "Failed"/"Unknown" from systemd-analyze). A keyword form systemd does not know '
                           'fails the gate (an unknown Firmware form is silently false).',
                   firmware_swap={'from': 'apple,j616s', 'to': 'the guest compatible, e.g. linux,dummy-virt'},
                   known_firmware_forms=['device-tree', 'device-tree-compatible(<value>)', 'uefi', 'smbios-field(<field> <op> <value>)'],
                   conditions=rows)
        out.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
        self.assertEqual(json.loads(out.read_text())['conditions'], rows)
        self.assertTrue(any(r['gate'] == 'must-succeed' and r['key'] == 'ConditionFirmware' for r in rows))


class F3RealHost(unittest.TestCase):
    def host_attrs(self, path):
        tree = ast.parse(path.read_text())
        used = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'host'
                    and isinstance(node.ctx, ast.Load)):
                used.add(node.attr)
        return used

    def defined(self, module):
        names = set(dir(module.Host))
        # attributes assigned in __init__ count too
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if isinstance(node, ast.ClassDef) and node.name == 'Host':
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == 'self' and isinstance(sub.ctx, ast.Store):
                        names.add(sub.attr)
        return names

    def test_usb_avd_and_aop_hosts_have_what_they_read(self):
        # usb/attach.py (F3's original subject) became usb/usb-test.py with the USB boot-time start
        for rel, name in (('usb/usb-test.py', 'usbtest_real'), ('avd/avd-test.py', 'avdtest_real')):
            m = load(name, TESTS/rel)
            missing = sorted(a for a in self.host_attrs(TESTS/rel) if a not in self.defined(m))
            self.assertEqual(missing, [], rel)


class F4Avd(unittest.TestCase):
    def setUp(self):
        self.m = load('avdtest_n2', TESTS/'avd/avd-test.py')

    def facts(self, status):
        return {rel: dict(present=True, status=status, compatible=['apple,t6040-x', c], expected=c) for rel, c in self.m.DT_NODES}

    def test_enabled_nodes_are_accepted_disabled_ones_are_an_old_tree(self):
        # m4.17: the loader stamp is gone and the nodes are enabled in the shipped tree (no status, or okay)
        self.m.check_dt(self.facts(None))
        self.m.check_dt(self.facts('okay'))
        with self.assertRaises(self.m.Refused):
            self.m.check_dt(self.facts('disabled'))

    def test_the_nodes_themselves_are_still_required(self):
        facts = self.facts(None)
        first = self.m.DT_NODES[0][0]
        facts[first]['present'] = False
        with self.assertRaises(self.m.Refused):
            self.m.check_dt(facts)

    def run_firmware(self, owner, qkk_rc, data=b'FW' * 100):
        with tempfile.TemporaryDirectory() as d:
            fw = Path(d)/'avd-fw-v5-t1.bin'
            fw.write_bytes(data)
            self.m.FIRMWARE = fw
            seen = []

            class Host:
                def run(self, argv, timeout=20, env=None):
                    seen.append(argv)
                    if argv[1] == '-Qqo':
                        return (0, owner + '\n', '') if owner else (1, '', 'error: No package owns %s\n' % argv[2])
                    return qkk_rc, 'avd-fw: 1 total files, 0 altered files\n' if qkk_rc == 0 else 'warning: avd-fw: backup\n', ''
            return self.m.firmware_facts(Host()), seen

    def test_the_package_owned_clean_file_is_accepted_and_its_hash_recorded(self):
        facts, seen = self.run_firmware('avd-fw', 0)
        self.assertEqual(facts['package'], 'avd-fw')
        self.assertEqual(facts['sha256'], self.m.sha(b'FW' * 100))
        self.assertEqual([a[1] for a in seen], ['-Qqo', '-Qkk'])
        self.assertEqual(seen[1][2], 'avd-fw')

    def test_a_file_of_another_package_or_none_is_refused(self):
        for owner in ('mesa', ''):
            with self.assertRaises(self.m.Refused) as ctx:
                self.run_firmware(owner, 0)
            self.assertIn('not owned by the avd-fw package', str(ctx.exception))

    def test_a_modified_file_is_refused(self):
        with self.assertRaises(self.m.Refused) as ctx:
            self.run_firmware('avd-fw', 1)
        self.assertIn('pacman -Qkk avd-fw is not clean', str(ctx.exception))

    def test_the_r_series_hash_pin_is_gone(self):
        text = (TESTS/'avd/avd-test.py').read_text()
        self.assertNotIn('cd0de0b9', text)
        self.assertNotIn('FIRMWARE_SHA256', text)


class F6Status(unittest.TestCase):
    def test_this_boot_line_says_entry_once(self):
        m = load('m4boot_n2', SUPPORT/'usr/bin/m4-boot')
        with tempfile.TemporaryDirectory() as d:
            chosen = Path(d)/'chosen'
            chosen.mkdir()
            for k, v in (('m4,boot-source', 'entry'), ('m4,boot-entry', '7.1.13-m4'), ('m4,boot-reason', 'default')):
                (chosen/k).write_bytes(v.encode() + b'\0')
            (Path(d)/'boot/m4').mkdir(parents=True)
            cmdline = Path(d)/'cmdline'
            cmdline.write_text('root=x\n')
            b = m.Boot(str(Path(d)/'boot'), str(Path(d)/'modules'), out=lambda s: None, chosen=str(chosen), proc_cmdline=str(cmdline),
                       rescue_file=str(Path(d)/'none'))
            lines = b.boot_report()
        self.assertEqual(lines[0], 'this boot: entry 7.1.13-m4 (reason default)')
        self.assertFalse(any('entry entry' in l for l in lines))


class F7BluetoothConf(unittest.TestCase):
    def test_the_default_config_ships_with_the_headset_address(self):
        conf = SUPPORT/'etc/m4/bluetooth.conf'
        self.assertIn('device=@M4_BT_HEADSET@', conf.read_text().splitlines())
        pkgbuild = (PKG/'m4-support/PKGBUILD').read_text()
        self.assertIn('etc/m4/bluetooth.conf', re.search(r'backup=\(([^)]*)\)', pkgbuild).group(1))
        unit = (SUPPORT/'usr/lib/systemd/system/m4-bt-connect.service').read_text()
        self.assertIn('ConditionPathExists=/etc/m4/bluetooth.conf', unit)

    def test_bt_connect_reads_the_configured_device(self):
        src = (SUPPORT/'usr/lib/m4/bt-connect').read_text()
        # the module imports bt_keyshare from /usr/lib/m4: run it with the source directory on the path
        sys.path.insert(0, str(SUPPORT/'usr/lib/m4'))
        try:
            m = load('btconnect_n2', SUPPORT/'usr/lib/m4/bt-connect')
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)/'bluetooth.conf'
            shipped = (SUPPORT/'etc/m4/bluetooth.conf').read_text()
            conf.write_text(shipped)
            m.CONF = conf
            self.assertEqual(m.configured_device(), re.search(r'^device=(.*)$', shipped, re.M).group(1))
            conf.write_text('device=AA:BB:CC:DD:EE:FF\n')
            self.assertEqual(m.configured_device(), 'AA:BB:CC:DD:EE:FF')
            conf.write_text('device=\ncontroller=\n')      # a machine-neutral build: nothing to connect
            self.assertEqual(m.configured_device(), '')
        self.assertNotIn('open(', src)             # nothing in this helper opens a file by itself (the key file is bt_keyshare's)

    def test_keyshare_does_nothing_without_both_addresses(self):
        sys.path.insert(0, str(SUPPORT/'usr/lib/m4'))
        try:
            ks = load('bt_keyshare_n2', SUPPORT/'usr/lib/m4/bt_keyshare.py')
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)/'bluetooth.conf'
            conf.write_text('device=AA:BB:CC:DD:EE:FF\ncontroller=\n# controller=00:00:00:00:00:00\n')
            self.assertEqual(ks.conf_values(conf), {'device': 'AA:BB:CC:DD:EE:FF'})
        ks.HOST_ADDRESS, ks.TARGET = '', 'AA:BB:CC:DD:EE:FF'
        self.assertEqual(ks.import_before_daemon(), dict(status='NOT_CONFIGURED', used=False))


if __name__ == '__main__':
    unittest.main(verbosity=1)
