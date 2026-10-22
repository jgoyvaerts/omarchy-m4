#!/usr/bin/python3 -I
"""Static and behavioural checks of the shipped configuration: Bluetooth load path (N1-REV-01), Wi-Fi firmware (N1-REV-03), mkinitcpio hooks,
safe-mode conditions.   python3 -B -I test_userspace_config.py   (trees: ../support, ../firmware, ../tools)"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TREES = Path(os.environ.get('M4_TREES', Path(__file__).resolve().parents[1]))
SUPPORT, FIRMWARE = TREES/'support', TREES/'firmware'
UNITS = SUPPORT/'usr/lib/systemd/system'
WIFI = ('brcmfmac4388c0-pcie.apple,hostomel-WLMT-a.txt', 'brcmfmac4388c0-pcie.apple,hostomel-WLMT-u.txt', 'brcmfmac4388c0-pcie.apple,hostomel.bin',
        'brcmfmac4388c0-pcie.apple,hostomel.clm_blob', 'brcmfmac4388c0-pcie.apple,hostomel.sig', 'brcmfmac4388c0-pcie.apple,hostomel.txcap_blob')


def options(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith('#')]


class ModprobeConfig(unittest.TestCase):
    def test_drivers_the_kernel_brings_up_autoload_except_on_a_rescue_boot(self):
        # 7.1.12-m4.10: the GPU node is enabled, appledrm takes over by default, the SMC children and hci_bcm4377 (which waits for the
        # Wi-Fi bind itself) need no unit
        lines = options((SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text())
        for module in ('asahi', 'appledrm', 'macsmc_input', 'macsmc_hwmon'):
            self.assertNotIn('blacklist ' + module, lines)
            self.assertIn("install %s /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || exec /usr/bin/modprobe --ignore-install %s'"
                          % (module, module), lines)
        bt = [l for l in lines if l.startswith('install hci_bcm4377 ')]
        self.assertEqual(len(bt), 1)
        self.assertIn("grep -qsw m4.safe /proc/cmdline && exit 0;", bt[0])
        self.assertIn('/sys/bus/pci/drivers/brcmfmac/', bt[0])
        self.assertTrue(bt[0].endswith("exec /usr/bin/modprobe --ignore-install hci_bcm4377'"), bt[0])

    def test_no_options_for_parameters_or_modules_the_aurora_kernels_lack(self):
        lines = options((SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text())
        for word in ('allow_bcm4388', 'm4_idle', 'm4_smc_hwmon'):
            self.assertEqual([l for l in lines if word in l], [], word)

    def test_the_retired_g16_gpu_driver_is_blacklisted_without_options(self):
        # m4_gpu_driver still exists in the Gravity kernels: it must never autoload next to asahi, and nothing configures it
        self.assertFalse((SUPPORT/'usr/lib/modprobe.d/m4-gpu.conf').exists())
        conf = ''.join(p.read_text() for p in (SUPPORT/'usr/lib/modprobe.d').glob('*.conf'))
        self.assertIn('blacklist m4_gpu_driver', options(conf))
        self.assertEqual([l for l in options(conf) if 'm4_gpu_driver' in l and l.startswith('options')], [])

    def test_cpufreq_does_not_autoload(self):
        # gaps G1: the board DT has cpufreq nodes; the driver loads by name only (m4-cpufreq.service)
        lines = options((SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text())
        self.assertIn('blacklist apple_soc_cpufreq', lines)


class PermitValues(unittest.TestCase):
    """The B21 class: a module a unit loads at boot with a permit/enable parameter has the value in modprobe.d; an attended-only module is listed."""
    def test_boot_modules_have_their_value_in_modprobe_d(self):
        conf = ''.join(p.read_text() for p in (SUPPORT/'usr/lib/modprobe.d').glob('*.conf'))
        for module, parameter in (('watchdog', 'handle_boot_enabled'),):
            self.assertRegex(conf, r'(?m)^options %s .*\b%s=' % (module, parameter), (module, parameter))

    def test_attended_only_modules_are_listed_with_who_passes_the_value(self):
        rows = [l.split('\t') for l in (SUPPORT/'usr/share/m4/attended-modules.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        listed = {r[0]: r for r in rows}
        for module in ('m4_bt_otp', 'm4_smc_telemetry'):
            self.assertIn(module, listed)
            self.assertIn('attended only', listed[module][2])
        conf = ''.join(p.read_text() for p in (SUPPORT/'usr/lib/modprobe.d').glob('*.conf'))
        for module in listed:      # never both: an attended-only module has no standing options line
            self.assertNotRegex(conf, r'(?m)^options %s ' % module)


class VideoDecoderAtBoot(unittest.TestCase):
    """m4.17: apple_avd autoloads at boot (no shim, no unit); a rescue boot never loads it, and nothing blacklists it."""
    def test_the_decoder_is_not_blacklisted_and_m4_safe_skips_it(self):
        conf = (SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text()
        self.assertNotRegex(conf, r'(?m)^blacklist (apple_avd|m4_avd_attach)\b')
        self.assertIn("install apple_avd /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || "
                      "exec /usr/bin/modprobe --ignore-install apple_avd'", conf)
        listed = (SUPPORT/'usr/share/m4/attended-modules.txt').read_text()
        self.assertNotIn('m4_avd_attach', listed)


class AopAtBoot(unittest.TestCase):
    """m4.10: the AOP stack autoloads at boot (no shim, no unit, no marker); a rescue boot never loads it, and nothing blacklists it."""
    MODULES = ('aop', 'aop_als', 'aop_las', 'snd_soc_aop', 'apple_admac')

    def test_the_aop_stack_is_not_blacklisted_and_m4_safe_skips_it(self):
        conf = (SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text()
        self.assertNotRegex(conf, r'(?m)^blacklist (m4_aop_attach|aop|aop_als|aop_las|snd_soc_aop|apple_admac)\b')
        for module in self.MODULES:
            self.assertIn("install %s /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || "
                          "exec /usr/bin/modprobe --ignore-install %s'" % (module, module), conf)
        self.assertFalse((UNITS/'m4-aop.service').exists())
        self.assertFalse((SUPPORT/'usr/lib/m4/aop/attach.py').exists())
        self.assertNotIn('m4_aop_attach', (SUPPORT/'usr/share/m4/attended-modules.txt').read_text())
        mic = (SUPPORT/'usr/share/wireplumber/wireplumber.conf.d/90-m4-aop-mic.conf').read_text()
        self.assertIn('alsa.driver_name = "snd_soc_aop"', mic)      # the card's ALSA driver name ("aop_audio" never matched)
        self.assertNotIn('"aop_audio"', mic)

    def test_one_ambient_light_controller_on_by_default(self):
        wants = SUPPORT/'usr/lib/systemd/user/graphical-session.target.wants/m4-als-brightness.service'
        self.assertEqual(os.readlink(wants), '../m4-als-brightness.service')
        self.assertFalse((SUPPORT/'usr/lib/m4/als-auto.py').exists())
        self.assertNotIn('als-auto', (SUPPORT/'usr/share/m4/hyprland.lua').read_text())


class UsbAtBoot(unittest.TestCase):
    """The right-hand USB-C port binds at boot (no shim, no gate): its three drivers autoload, a rescue boot never loads them, and
    nothing blacklists them."""
    MODULES = ('phy_apple_t6040_usb2', 'dwc3_apple', 'sn201202x')

    def test_the_usb_stack_is_not_blacklisted_and_m4_safe_skips_it(self):
        conf = (SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text()
        self.assertNotRegex(conf, r'(?m)^blacklist (m4_usb_attach|phy_apple_t6040_usb2|dwc3_apple|dwc3|sn201202x|tps6598x_core|'
                                  r'xhci_plat_hcd|xhci_hcd)\b')
        for module in self.MODULES:
            self.assertIn("install %s /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || "
                          "exec /usr/bin/modprobe --ignore-install %s'" % (module, module), conf)
        self.assertNotIn('m4_usb_attach', (SUPPORT/'usr/share/m4/attended-modules.txt').read_text())
        for module in self.MODULES + ('xhci_plat_hcd',):
            self.assertNotRegex(conf, r'(?m)^options %s .*m4_admit' % module)


class SpeakersAtBoot(unittest.TestCase):
    """m4.11 (gaps G7): the speaker stack autoloads at boot, a rescue boot never loads it, and nothing blacklists it; the DSP chain,
    the UCM and speakersafetyd's J616 profile ship with it."""
    MODULES = ('i2c_pasemi_platform', 'snd_soc_tas2764', 'snd_soc_macaudio')
    SHARE = SUPPORT/'usr/share'

    def test_the_speaker_stack_is_not_blacklisted_and_m4_safe_skips_it(self):
        conf = (SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text()
        self.assertNotRegex(conf, r'(?m)^blacklist (i2c_pasemi_platform|snd_soc_tas2764|snd_soc_macaudio)\b')
        self.assertNotRegex(conf, r'(?m)^options (snd_soc_aop|snd_soc_macaudio|snd_soc_tas2764) ')     # the kernel holds the values
        for module in self.MODULES:
            self.assertIn("install %s /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || "
                          "exec /usr/bin/modprobe --ignore-install %s'" % (module, module), conf)

    def test_the_package_needs_the_capped_kernel_and_the_audio_stack(self):
        pkgbuild = (TREES.parent/'packages/m4-support/PKGBUILD').read_text()
        for dep in ("'linux-m4>=7.1.12-11'", 'asahi-audio', 'speakersafetyd', 'alsa-ucm-conf-asahi'):
            self.assertIn(dep, pkgbuild)

    def test_speakersafetyd_starts_for_j616_with_six_speakers_at_the_macos_limits(self):
        rule = (SUPPORT/'usr/lib/udev/rules.d/95-m4-speakersafetyd.rules').read_text()
        self.assertIn('ATTRS{id}=="AppleJ616"', rule)
        self.assertIn('ENV{SYSTEMD_WANTS}="speakersafetyd.service"', rule)
        # parent keys of one rule must all match one parent: id is on the card, the driver on the platform device (it never matched)
        self.assertNotRegex(rule, r'(?m)^[^#].*ATTRS\{id\}.*DRIVERS==')
        conf = (self.SHARE/'speakersafetyd/apple/j616.conf').read_text()
        speakers = re.findall(r'(?m)^\[Speaker/(.+)\]$', conf)
        self.assertEqual(speakers, ['Left Woofer 1', 'Right Woofer 1', 'Left Woofer 2', 'Right Woofer 2', 'Left Tweeter', 'Right Tweeter'])
        self.assertEqual(re.findall(r'(?m)^t_limit = (.+)$', conf), ['140.0'] * 6)
        chans = [(int(i), int(v)) for i, v in re.findall(r'(?m)^is_chan = (\d+)\nvs_chan = (\d+)$', conf)]
        self.assertEqual(chans, [(2 * k, 2 * k + 1) for k in range(6)])

    def test_the_dsp_graph_feeds_the_j616_slots_left_and_right(self):
        graph = (self.SHARE/'m4/speakers/j616-graph.json').read_text()
        outputs = re.search(r'"outputs": \[(.*?)\]', graph, re.S).group(1)
        self.assertEqual(re.findall(r'"(\w+):Out"', outputs), ['copyLW', 'copyRW', 'copyLW2', 'copyRW2', 'convLT', 'convRT'])
        # the owner's ear (2026-10-20): the J316 balance is bright on J616; the tweeter convolvers run at -6 dB until J616 FIRs exist
        self.assertEqual(re.findall(r'"name": "conv(LT|RT)".*?"gain": ([\d.]+)', graph, re.S), [('LT', '0.5'), ('RT', '0.5')])
        rule = (self.SHARE/'wireplumber/wireplumber.conf.d/91-m4-speakers.conf').read_text()
        self.assertIn('api.alsa.path = "hw:AppleJ616,1"', rule)
        self.assertIn('filter-path = "/usr/share/m4/speakers/j616-graph.json"', rule)
        hifi = (self.SHARE/'alsa/ucm2/conf.d/macaudio/j616/HiFi.conf').read_text()
        self.assertIn('PlaybackPCM "hw:${CardId},1"', hifi)
        self.assertNotIn('Headphone', hifi)       # the J616 card has no jack codec
        self.assertIn('File "/conf.d/macaudio/j616/HiFi.conf"', (self.SHARE/'alsa/ucm2/conf.d/macaudio/MacBook Pro J616.conf').read_text())


    def test_the_macos_graph_is_the_j316_graph_with_the_owners_firs_at_one_level(self):
        load = lambda name: json.loads(re.sub(r',(\s*[}\]])', r'\1', (self.SHARE/'m4/speakers'/name).read_text()))
        j316, macos = load('j616-graph.json'), load('j616-graph-macos.json')
        convs = {n['name']: n['config'] for n in macos['filter.graph']['nodes'] if n.get('label') == 'convolver'}
        self.assertEqual({k: (c['filename'], c['gain']) for k, c in convs.items()},
                         {'conv%s' % s: ('/etc/m4/speakers/j616-%s-48000.wav' % k, 1.603)       # owner A/B 2026-10-21: "B is fine"
                          for s, k in (('LT', 'tweeter'), ('RT', 'tweeter'), ('LW', 'woofer'), ('RW', 'woofer'))})
        # one gain on all four: the kit's woofer/tweeter balance stays, the woofer band matches the J316 graph's level
        for g in (j316, macos):
            for n in g['filter.graph']['nodes']:
                n.pop('config', None) if n.get('label') == 'convolver' else None
        self.assertEqual(j316, macos)

class HeadphoneJack(unittest.TestCase):
    """gaps G3: the jack is a sound card of its own (macaudio "MacBook Pro J616 Jack", id AppleJack). Its UCM and WirePlumber rule
    only match that card, so they are inert until the kernel registers it, and the speaker card's files stay jack-free."""
    SHARE = SUPPORT/'usr/share'

    def test_the_jack_card_has_headphones_and_a_headset_mic_on_pcm_0(self):
        ucm = self.SHARE/'alsa/ucm2/conf.d/macaudio'
        self.assertIn('File "/conf.d/macaudio/j616/Jack.conf"', (ucm/'MacBook Pro J616 Jack.conf').read_text())
        jack = (ucm/'j616/Jack.conf').read_text()
        self.assertEqual(re.findall(r'(?:Playback|Capture)PCM "([^"]+)"', jack), ['hw:${CardId},0'] * 2)
        self.assertEqual(re.findall(r'JackControl "([^"]+)"', jack), ['Headphone Jack', 'Headset Mic Jack'])
        self.assertNotIn("'Preamp Gain'", jack)      # asahi's name; this kernel's control is "Jack ADC Preamp Capture Volume"
        self.assertNotIn('Speaker Playback Mux', jack)

    def test_the_plugged_jack_outranks_the_speakers_and_the_aop_mics(self):
        rule = (self.SHARE/'wireplumber/wireplumber.conf.d/92-m4-jack.conf').read_text()
        self.assertEqual(rule.count('api.alsa.path = "hw:AppleJack,0"'), 2)
        sink, source = (int(p) for p in re.findall(r'priority\.session = (\d+)', rule))
        self.assertGreater(sink, 850)       # the J616 speaker filter (j616-graph.json)
        self.assertGreater(source, 2000)    # WirePlumber's default for capture device 0 (the AOP microphones)


class WatchdogOffAndPanicReboots(unittest.TestCase):
    """No unit opens or pets the watchdog (apple_wdt binds since linux-m4 7.1.12-13; only systemd's RebootWatchdogSec uses it), and a panic
    reboots into the rescue set through boot-pending."""
    def test_no_unit_arms_a_watchdog(self):
        self.assertFalse((UNITS/'m4-watchdog.service').exists())
        self.assertFalse((SUPPORT/'usr/lib/m4/watchdog-heartbeat').exists())
        for p in SUPPORT.rglob('*'):
            if p.is_file() and not p.is_symlink() and p.stat().st_size < 1_000_000:
                try:
                    text = p.read_text()
                except UnicodeDecodeError:
                    continue
                self.assertNotRegex(text, r'(?m)^[^#\n]*(/dev/watchdog|RuntimeWatchdogSec|WatchdogDevice)', str(p))
        # apple_wdt binds since linux-m4 7.1.12-13 (DT node); nothing in the package opens it or sets a runtime watchdog
        self.assertNotIn('blacklist apple_wdt', options((SUPPORT/'usr/lib/modprobe.d/m4.conf').read_text()))

    def test_the_command_line_reboots_on_panic(self):
        words = (SUPPORT/'usr/share/m4/cmdline').read_text().split()
        self.assertIn('panic=-1', words)
        self.assertNotIn('idle=nop', words)      # the aurora kernels idle with the register-saving wfi from boot
        self.assertLessEqual(len(' '.join(words)), 1024)


class MtestRefusals(unittest.TestCase):
    """`m4-test ROW` on a bare system: one named reason, exit 1 or 2, no traceback (the packaged dispatcher, no hardware, no systemd)."""
    def run_row(self, *argv, user=False):
        env = dict(os.environ, PYTHONPATH=str(SUPPORT/'usr/lib/m4'), PATH='/nonexistent')
        cmd = [sys.executable, str(TREES/'tools/usr/bin/m4-test'), *argv]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)

    def test_every_row_refuses_cleanly(self):
        listing = subprocess.run([sys.executable, str(TREES/'tools/usr/bin/m4-test'), 'list'], capture_output=True, text=True,
                                 env=dict(os.environ, PYTHONPATH=str(SUPPORT/'usr/lib/m4'))).stdout
        rows = [l.strip() for l in listing.splitlines() if l.startswith('  ')]
        self.assertGreaterEqual(len(rows), 13)
        for row in rows:
            p = self.run_row(row)
            self.assertNotIn('Traceback', p.stdout + p.stderr, row)
            self.assertIn(p.returncode, (1, 2), row)
            self.assertTrue((p.stdout + p.stderr).strip(), row)


class Bluetooth(unittest.TestCase):
    def test_the_units_order_on_bluetoothd_only(self):
        # hci_bcm4377 autoloads (its probe waits for the Wi-Fi bind): no load unit, nothing orders on one
        self.assertFalse((UNITS/'m4-bluetooth-load.service').exists())
        self.assertFalse((SUPPORT/'usr/lib/m4/bluetooth-load').exists())
        self.assertRegex((UNITS/'m4-bt-connect.service').read_text(), r'(?m)^After=bluetooth\.service$')
        self.assertRegex((UNITS/'m4-bt-keyshare.service').read_text(), r'(?m)^Before=bluetooth\.service$')

    def test_nothing_unloads_the_driver(self):
        for p in SUPPORT.rglob('*'):
            if p.is_file() and not p.is_symlink() and p.stat().st_size < 1_000_000:
                try:
                    text = p.read_text()
                except UnicodeDecodeError:
                    continue
                code = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#'))
                self.assertNotRegex(code, r'(modprobe\s+-r|rmmod)\s+[^\n]*hci_bcm4377', str(p))


@unittest.skipUnless((FIRMWARE/'usr/lib/firmware/brcm').is_dir(), 'firmware files are not in git: run tools/collect-firmware.sh first')
class WifiFirmware(unittest.TestCase):
    def test_the_six_brcmfmac_files_are_shipped_and_match_their_provenance(self):
        prov = json.loads((FIRMWARE/'usr/share/licenses/m4-firmware/brcmfmac-provenance.json').read_text())
        shipped = FIRMWARE/'usr/lib/firmware/brcm'
        self.assertEqual({Path(k).name for k in prov['files']}, set(WIFI))
        for rel, row in prov['files'].items():
            data = (shipped/Path(rel).name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row['sha256'], rel)
            self.assertEqual(len(data), row['bytes'], rel)

    def test_regulatory_db_is_not_shipped(self):
        names = {p.name for p in FIRMWARE.rglob('*') if p.is_file()}
        self.assertFalse({'regulatory.db', 'regulatory.db.p7s'} & names)

    def test_checksum_list_covers_every_firmware_file(self):
        listed = {l.split('  ', 1)[1] for l in (FIRMWARE/'FIRMWARE.sha256').read_text().splitlines() if l.strip()}
        present = {str(p.relative_to(FIRMWARE)) for p in (FIRMWARE/'usr/lib/firmware').rglob('*') if p.is_file()}
        self.assertEqual(listed, present)
        for l in (FIRMWARE/'FIRMWARE.sha256').read_text().splitlines():
            digest, name = l.split('  ', 1)
            self.assertEqual(hashlib.sha256((FIRMWARE/name).read_bytes()).hexdigest(), digest, name)

    def test_names_are_what_the_driver_composes(self):
        # brcmfmac (pcie.c): <chip "brcmfmac4388c0-pcie">.<board_type "apple,hostomel"[-<OTP module>-<vendor>]>.{bin,clm_blob,txcap_blob,sig,txt}
        # board_types[2] = "<board>-<module>-<vendor>" with module WLMT and vendor a|u from the OTP; files are tried most specific first.
        for name in WIFI:
            self.assertRegex(name, r'^brcmfmac4388c0-pcie\.apple,hostomel(-WLMT-[au])?\.(bin|clm_blob|txcap_blob|sig|txt)$')


class MkinitcpioConfig(unittest.TestCase):
    def hooks(self, path):
        m = re.search(r'(?m)^HOOKS=\(([^)]*)\)', path.read_text())
        return m.group(1).split()

    def test_shutdown_hook_is_in_both_images(self):
        for conf in (SUPPORT/'etc/m4/mkinitcpio.conf', SUPPORT/'usr/share/m4/rescue-mkinitcpio.conf'):
            hooks = self.hooks(conf)
            self.assertIn('shutdown', hooks, conf)
            self.assertIn('m4-clock', hooks)
            self.assertNotIn('udev', hooks)            # its hwdb alone is 14 MB of the 16 MiB window
        self.assertIn('m4-rescue', self.hooks(SUPPORT/'usr/share/m4/rescue-mkinitcpio.conf'))


class SafeMode(unittest.TestCase):
    def test_every_unit_that_loads_a_hardware_module_has_the_safe_condition(self):
        for name in ('m4-gpu', 'm4-display', 'm4-kbd-idle', 'm4-bt-keyshare', 'm4-bt-connect', 'm4-cpufreq'):
            text = (UNITS/(name + '.service')).read_text()
            self.assertIn('ConditionKernelCommandLine=!m4.safe', text, name)
            self.assertIn('m4-boot-pending.service', text, name)


class Container(unittest.TestCase):
    """HP boots as a container on this machine: no unit that m4.safe keeps off may run there either (the host's hardware, and HP's own
    /boot/m4/boot-pending, which would send HP's next native boot to the rescue set)."""

    def test_every_unit_that_m4_safe_keeps_off_skips_containers(self):
        units = [p for p in SUPPORT.rglob('*.service') if not p.is_symlink() and 'ConditionKernelCommandLine=!m4.safe' in p.read_text()]
        self.assertGreaterEqual(len(units), 9)
        for p in units:
            self.assertIn('ConditionVirtualization=!container', p.read_text().splitlines(), p.name)


class GpuIdleHold(unittest.TestCase):
    """G-13: udev sets the kernel's GPU idle hold from the power source (AC 20 ms, battery the firmware's 2 ms; /etc/m4/gpu-idle.conf
    overrides) at every charger change and GPU bind."""
    SCRIPT = SUPPORT/'usr/lib/m4/gpu-idle-power'

    def run_script(self, online, conf=None, knob=True):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bin_dir = root/'bin'
            bin_dir.mkdir()
            (bin_dir/'logger').write_text('#!/bin/sh\n')            # keep test runs out of the journal
            (bin_dir/'logger').chmod(0o755)
            (root/'sys/class/power_supply/macsmc-ac').mkdir(parents=True)
            (root/'sys/class/power_supply/macsmc-ac/online').write_text(online + '\n')
            gpu = root/'sys/bus/platform/drivers/asahi/2fa000000.gpu'
            gpu.mkdir(parents=True)
            if knob:
                (gpu/'idle_off_delay_ms').write_text('2\n')
            if conf is not None:
                (root/'etc/m4').mkdir(parents=True)
                (root/'etc/m4/gpu-idle.conf').write_text(conf)
            env = {'PATH': '%s:/usr/bin:/bin' % bin_dir, 'M4_GPU_IDLE_ROOT': str(root)}
            done = subprocess.run([str(self.SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return (gpu/'idle_off_delay_ms').read_text().strip() if knob else None

    def test_the_power_source_picks_the_delay_and_the_conf_overrides_it(self):
        self.assertEqual(self.run_script('1'), '20')
        self.assertEqual(self.run_script('0'), '2')
        self.assertEqual(self.run_script('1', 'AC_MS=8\nBATTERY_MS=4\n'), '8')
        self.assertEqual(self.run_script('0', 'AC_MS=8\nBATTERY_MS=4\n'), '4')
        self.assertIsNone(self.run_script('1', knob=False))                # a kernel without the knob: nothing to do

    def test_udev_runs_the_packaged_script_on_charger_change_and_gpu_bind(self):
        rule = (SUPPORT/'usr/lib/udev/rules.d/90-m4-gpu-idle-power.rules').read_text()
        self.assertIn('SUBSYSTEM=="power_supply", KERNEL=="macsmc-ac", ACTION=="add|change", RUN+="/usr/lib/m4/gpu-idle-power"', rule)
        self.assertIn('SUBSYSTEM=="platform", DRIVER=="asahi", ACTION=="bind", RUN+="/usr/lib/m4/gpu-idle-power"', rule)
        self.assertIn('gpu-idle-power', (TREES.parent/'packages/m4-support/PKGBUILD').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=1)
