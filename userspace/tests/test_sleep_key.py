#!/usr/bin/python3 -I
"""The moon key (Do Not Disturb, reported as KEY_SLEEP) must not suspend: m4-support's logind drop-in, as omarchy-mac ships it.
   python3 -B -I test_sleep_key.py   (tree: ../support)"""
import configparser
import os
import unittest
from pathlib import Path

SUPPORT = Path(os.environ.get('M4_TREES', Path(__file__).resolve().parents[1]))/'support'
DROPIN = SUPPORT/'usr/lib/systemd/logind.conf.d/m4-sleep-key.conf'


class SleepKey(unittest.TestCase):
    def test_moon_key_ignored(self):
        c = configparser.ConfigParser(strict=True)
        c.optionxform = str
        c.read_string(DROPIN.read_text())
        self.assertEqual(dict(c['Login']), {'HandleSuspendKey': 'ignore', 'HandleSuspendKeyLongPress': 'ignore'})

    def test_lid_and_power_key_stay_vanilla(self):
        for f in DROPIN.parent.iterdir():
            text = f.read_text()
            for key in ('HandleLidSwitch', 'HandlePowerKey'):
                self.assertNotIn(key, text, f.name)


if __name__ == '__main__':
    unittest.main()
