#!/usr/bin/python3 -I
"""gaps G1 capped interim: /usr/lib/m4/cpufreq-start against a fake sysfs tree. Every refusal must happen before anything is
written, and the caps must be written before the governor.   python3 -B -I test_g1_cpufreq_start.py"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

TREES = Path(__file__).resolve().parents[1]
SCRIPT = TREES/'support/usr/lib/m4/cpufreq-start'
UNIT = TREES/'support/usr/lib/systemd/system/m4-cpufreq.service'
E_KHZ = [1020000, 1404000, 1788000, 2112000, 2352000, 2532000, 2592000]
P_KHZ = [1260000, 1512000, 1800000, 2088000, 2352000, 2616000, 2868000, 3096000, 4512000]


def load():
    loader = importlib.machinery.SourceFileLoader('cpufreq_start', str(SCRIPT))
    spec = importlib.util.spec_from_loader('cpufreq_start', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Tree:
    def __init__(self, root, conf='P_MAX_KHZ=2616000\nE_MAX_KHZ=2112000\n', temp='52000', bound=('cpufreq-cpu0', 'cpufreq-cpu1',
                 'cpufreq-cpu5'), policies=True, zone=True, layout=(('policy0', '0 10 11 12 13'), ('policy1', '1 2 3 4'),
                 ('policy5', '5 6 7 8 9'))):
        self.root = Path(root)
        if conf is not None:
            (self.root/'etc/m4').mkdir(parents=True)
            (self.root/'etc/m4/cpufreq.conf').write_text(conf)
        th = self.root/'sys/class/thermal'
        th.mkdir(parents=True)
        cdevs = tuple('cpufreq-cpu' + cpus.split()[0] for _, cpus in layout)
        for i, name in enumerate(cdevs + ('battery',)):
            (th/('cooling_device%d' % i)).mkdir()
            (th/('cooling_device%d' % i)/'type').write_text(name + '\n')
        if zone:
            z = th/'thermal_zone0'
            z.mkdir()
            for k, v in (('type', 'cpu-die-thermal'), ('temp', temp), ('trip_point_0_type', 'passive'),
                         ('trip_point_0_temp', '113000')):
                (z/k).write_text(v + '\n')
            (z/'cdev0_trip_point').write_text('0\n')
            for i, name in enumerate(cdevs):
                if name in bound:
                    os.symlink('../cooling_device%d' % i, z/('cdev%d' % i))
        self.cpufreq = self.root/'sys/devices/system/cpu/cpufreq'
        self.cpufreq.mkdir(parents=True)
        if policies:
            for p, cpus in layout:
                table = E_KHZ if p == 'policy1' else P_KHZ
                d = self.cpufreq/p
                d.mkdir()
                (d/'related_cpus').write_text(cpus + '\n')
                (d/'scaling_available_frequencies').write_text(' '.join(map(str, table)) + ' \n')
                (d/'scaling_max_freq').write_text('%d\n' % table[-1])
                (d/'scaling_governor').write_text('powersave\n')

    def state(self):
        return {p: ((self.cpufreq/p/'scaling_max_freq').read_text().strip(), (self.cpufreq/p/'scaling_governor').read_text().strip())
                for p in ('policy0', 'policy1', 'policy5', 'policy9') if (self.cpufreq/p).is_dir()}


class Start(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.m = load()
        self.m.ROOT = Path(self.dir.name)
        self.m.WAIT_S = 0.3
        self.loaded, self.writes = [], []
        real_write = self.m.write

        def write(path, value):
            self.writes.append((str(path).rsplit('/', 2)[-2:], str(value)))
            real_write(path, value)
        self.m.write = write

    def tearDown(self):
        self.dir.cleanup()

    def run_start(self, sleep=lambda s: None):
        return self.m.start(run=lambda argv: self.loaded.append(argv[-1]) or 0, sleep=sleep)

    def defer_max_writes(self, apply_after):
        """scaling_max_freq lands only after `apply_after` polls, like the kernel's deferred policy update."""
        pending, polls = [], []
        real = (lambda path, value: (self.m.ROOT/path).write_text(str(value)))

        def write(path, value):
            self.writes.append((str(path).rsplit('/', 2)[-2:], str(value)))
            (pending.append if str(path).endswith('scaling_max_freq') else (lambda pv: real(*pv)))((path, value))
        self.m.write = write

        def sleep(seconds):
            polls.append(seconds)
            if apply_after is not None and len(polls) == apply_after:
                for path, value in pending:
                    real(path, value)
        return sleep

    def test_caps_applied_late_by_the_kernel_are_waited_for(self):
        t = Tree(self.dir.name)
        self.assertEqual(self.run_start(sleep=self.defer_max_writes(3)), 0)
        self.assertEqual(t.state(), {'policy0': ('2616000', 'schedutil'), 'policy1': ('2112000', 'schedutil'),
                                     'policy5': ('2616000', 'schedutil')})

    def test_caps_that_never_land_leave_the_governors_alone(self):
        t = Tree(self.dir.name)
        with self.assertRaises(self.m.Refused) as ctx:
            self.run_start(sleep=self.defer_max_writes(None))
        self.assertIn('caps on all three policies', str(ctx.exception))
        self.assertEqual({p: g for p, (m, g) in t.state().items()}, {'policy0': 'powersave', 'policy1': 'powersave',
                                                                     'policy5': 'powersave'})
        self.assertNotIn('scaling_governor', [w[0][1] for w in self.writes])

    LAYOUT_73 = (('policy0', '0 5 6 7 8'), ('policy1', '1 2 3 4'), ('policy9', '9 10 11 12 13'))

    def test_linux_7_3_layout_caps_then_schedutil(self):
        t = Tree(self.dir.name, bound=('cpufreq-cpu0', 'cpufreq-cpu1', 'cpufreq-cpu9'), layout=self.LAYOUT_73)
        self.assertEqual(self.run_start(), 0)
        self.assertEqual(t.state(), {'policy0': ('2616000', 'schedutil'), 'policy1': ('2112000', 'schedutil'),
                                     'policy9': ('2616000', 'schedutil')})

    def test_linux_7_3_layout_needs_its_cdev_bound(self):
        t = Tree(self.dir.name, bound=('cpufreq-cpu0', 'cpufreq-cpu1'), layout=self.LAYOUT_73)
        with self.assertRaises(self.m.Refused):
            self.run_start()
        self.assertEqual(t.state(), {'policy0': ('4512000', 'powersave'), 'policy1': ('2592000', 'powersave'),
                                     'policy9': ('4512000', 'powersave')})

    def test_mixed_layout_is_refused(self):
        t = Tree(self.dir.name, layout=(('policy0', '0 5 6 7 8'), ('policy1', '1 2 3 4'), ('policy5', '9 10 11 12 13')))
        with self.assertRaises(self.m.Refused):
            self.run_start()
        self.assertEqual(self.writes, [])

    def test_good_tree_caps_then_schedutil(self):
        t = Tree(self.dir.name)
        self.assertEqual(self.run_start(), 0)
        self.assertEqual(self.loaded, ['macsmc_hwmon', 'apple_soc_cpufreq'])
        self.assertEqual(t.state(), {'policy0': ('2616000', 'schedutil'), 'policy1': ('2112000', 'schedutil'),
                                     'policy5': ('2616000', 'schedutil')})
        kinds = [w[0][1] for w in self.writes]
        self.assertEqual(kinds, ['scaling_max_freq'] * 3 + ['scaling_governor'] * 3)

    def assertRefusedUntouched(self, tree, text):
        before = tree.state()
        with self.assertRaises(self.m.Refused) as ctx:
            self.run_start()
        self.assertIn(text, str(ctx.exception))
        self.assertEqual(self.writes, [])
        self.assertEqual(tree.state(), before)

    def test_no_conf_means_no_caps(self):
        t = Tree(self.dir.name, conf=None)
        self.assertEqual(self.run_start(), 0)
        self.assertEqual(t.state(), {'policy0': ('%d' % max(P_KHZ), 'schedutil'), 'policy1': ('%d' % max(E_KHZ), 'schedutil'),
                                     'policy5': ('%d' % max(P_KHZ), 'schedutil')})

    def test_conf_needs_both_caps(self):
        self.assertRefusedUntouched(Tree(self.dir.name, conf='P_MAX_KHZ=2616000\n'), 'needs P_MAX_KHZ and E_MAX_KHZ')

    def test_conf_rejects_other_lines(self):
        self.assertRefusedUntouched(Tree(self.dir.name, conf='P_MAX_KHZ=2616000\nE_MAX_KHZ=2112000\nGOVERNOR=performance\n'),
                                    'unreadable line')

    def test_cap_must_be_a_table_frequency(self):
        self.assertRefusedUntouched(Tree(self.dir.name, conf='P_MAX_KHZ=2600000\nE_MAX_KHZ=2112000\n'), 'is not one of')

    def test_no_zone_no_cpufreq(self):
        t = Tree(self.dir.name, zone=False)
        self.assertRefusedUntouched(t, 'cpu-die-thermal zone')
        self.assertEqual(self.loaded, ['macsmc_hwmon'])

    def test_insane_tcmz(self):
        self.assertRefusedUntouched(Tree(self.dir.name, temp='0'), 'not a die temperature')

    def test_zone_must_be_bound_to_all_three_policies(self):
        self.assertRefusedUntouched(Tree(self.dir.name, bound=('cpufreq-cpu0', 'cpufreq-cpu1')), 'zone binding')

    def test_policies_must_appear(self):
        self.assertRefusedUntouched(Tree(self.dir.name, policies=False), 'three cpufreq policies')


class Unit(unittest.TestCase):
    def test_conditions_and_link(self):
        lines = UNIT.read_text().splitlines()
        for line in ('ConditionFirmware=device-tree-compatible(apple,j616s)', 'ConditionKernelCommandLine=!m4.safe',
                     'ExecStart=/usr/bin/python3 -IB /usr/lib/m4/cpufreq-start'):
            self.assertIn(line, lines)
        link = UNIT.parent/'multi-user.target.wants/m4-cpufreq.service'
        self.assertTrue(link.is_symlink() and str(link.readlink()) == '../m4-cpufreq.service')

    def test_cpufreq_stays_blacklisted_and_the_sensor_autoloads_outside_rescue(self):
        conf = (TREES/'support/usr/lib/modprobe.d/m4.conf').read_text().splitlines()
        self.assertIn('blacklist apple_soc_cpufreq', conf)
        self.assertNotIn('blacklist macsmc_hwmon', conf)
        self.assertIn("install macsmc_hwmon /bin/sh -c 'grep -qsw m4.safe /proc/cmdline || "
                      "exec /usr/bin/modprobe --ignore-install macsmc_hwmon'", conf)


if __name__ == '__main__':
    unittest.main()
