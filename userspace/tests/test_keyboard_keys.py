#!/usr/bin/python3 -I
"""The built-in keyboard gets Omarchy's Print and keyboard-light keys through a udev hwdb remap (F4, F5, F6), as omarchy-mac does;
other keyboards keep theirs.   python3 -B -I test_keyboard_keys.py   (tree: ../support; needs systemd-hwdb, else skipped)"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SUPPORT = Path(os.environ.get('M4_TREES', Path(__file__).resolve().parents[1]))/'support'
HWDB = SUPPORT/'usr/lib/udev/hwdb.d/90-m4-keyboard.hwdb'
REMAP = {'KEYBOARD_KEY_7003d=sysrq', 'KEYBOARD_KEY_7003e=kbdillumdown', 'KEYBOARD_KEY_7003f=kbdillumup'}


@unittest.skipUnless(shutil.which('systemd-hwdb'), 'systemd-hwdb is not installed')
class KeyboardKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root/'usr/lib/udev/hwdb.d').mkdir(parents=True)
        shutil.copy(HWDB, root/'usr/lib/udev/hwdb.d')
        subprocess.run(['systemd-hwdb', '--root', root, 'update', '--usr', '--strict'], check=True)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def lookup(self, name):
        out = subprocess.run(['systemd-hwdb', '--root', self.root, 'query', 'evdev:name:%s:phys:514b28000.input.2:ev:120013' % name],
                             check=True, capture_output=True, text=True).stdout
        return set(out.split())

    def test_builtin_keyboard_remapped(self):
        for name in ('Apple MTP keyboard', 'Apple MTP Keyboard'):
            self.assertEqual(self.lookup(name), REMAP, name)

    def test_other_keyboards_keep_their_keys(self):
        for name in ('Apple Inc. Magic Keyboard', 'Keychron K2', 'Apple MTP multi-touch', 'Apple SPI Keyboard'):
            self.assertEqual(self.lookup(name), set(), name)


if __name__ == '__main__':
    unittest.main()
