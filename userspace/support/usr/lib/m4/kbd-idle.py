#!/usr/bin/python3 -B
"""Keyboard backlight off when idle. A small root service; no dependencies beyond the standard library.

an earlier version started a second `hypridle` for this from gpu/hyprland.lua. Omarchy 4.0.4 does not install hypridle (its idle
handling lives in quickshell), so that hook never ran. This service watches the internal keyboard and trackpad
(`/dev/input/event*`) itself:

  * after IDLE_S seconds without a key, pointer or touch event it saves the level of the `*kbd_backlight*` LED and
    sets 0;
  * on the first event afterwards it restores the saved level, but only if the level is still 0 (the owner or
    Omarchy may have changed it meanwhile: lock-screen blanking, the SUPER + brightness keys; those are never
    overridden), and it refreshes brightnessctl's saved slot of every user who has one for the LED (the desktop
    user, whatever their UID) so Omarchy's own later `restore` (after a lock blank that read our 0) restores the
    same level instead of 0;
  * if something sets 0 right after our restore with no further input (Omarchy's resume handler racing us), it
    re-asserts the level once; a later input event always wins;
  * it restores a level it dimmed when it is stopped (so systemd-backlight saves the real level at shutdown) and
    after a restart (the saved level lives in /run/m4-kbd-idle-saved).

Opt-out: the file /etc/m4/kbd-idle-off (checked while running too), or `systemctl disable m4-kbd-idle.service`.
The unit is `m4-kbd-idle.service` (Restart=on-failure).
"""
from pathlib import Path
import errno, glob, os, pwd, re, select, signal, struct, subprocess, sys, time

IDLE_S = 60.0
RESCAN_S = 5.0
REASSERT_S = 0.4
OPT_OUT = '/etc/m4/kbd-idle-off'
SAVED = '/run/m4-kbd-idle-saved'
RUN_USER = '/run/user'                    # brightnessctl keeps its saved slots in $XDG_RUNTIME_DIR/brightnessctl
EVENT = struct.Struct('llHHi')
EV_KEY, EV_REL, EV_ABS = 1, 2, 3
ACTIVITY = (EV_KEY, EV_REL, EV_ABS)       # not EV_SYN / EV_MSC / EV_LED
KEY_ENTER, KEY_A, KEY_SPACE, BTN_LEFT, BTN_TOUCH = 28, 30, 57, 0x110, 0x14a
ABS_X, ABS_Y, ABS_MT_POSITION_X = 0, 1, 0x35
REL_X, REL_Y = 0, 1
BUS_USB, BUS_BLUETOOTH, BUS_HOST, BUS_SPI = 0x03, 0x05, 0x19, 0x1c
INTERNAL_NAME = re.compile(r'^Apple (MTP|SPI|Internal)\b', re.I)


def bitmask(text):
    """sysfs capability words (most significant 64-bit word first) -> int."""
    value = 0
    for word in text.split():
        value = (value << 64) | int(word, 16)
    return value


def bit(mask, number):
    return (mask >> number) & 1


def read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def classify(device):
    """'keyboard', 'pointer' or None for /sys/class/input/eventN/device."""
    try:
        ev = bitmask(read(device/'capabilities/ev', '0'))
        key = bitmask(read(device/'capabilities/key', '0'))
        rel = bitmask(read(device/'capabilities/rel', '0'))
        absolute = bitmask(read(device/'capabilities/abs', '0'))
    except ValueError:
        return None
    if bit(ev, EV_KEY) and bit(key, KEY_A) and bit(key, KEY_SPACE) and bit(key, KEY_ENTER):
        return 'keyboard'
    touch = bit(ev, EV_ABS) and (bit(absolute, ABS_MT_POSITION_X) or (bit(absolute, ABS_X) and bit(absolute, ABS_Y)))
    if touch and bit(ev, EV_KEY) and bit(key, BTN_TOUCH):
        return 'pointer'
    if bit(ev, EV_REL) and bit(rel, REL_X) and bit(rel, REL_Y) and bit(ev, EV_KEY) and bit(key, BTN_LEFT):
        return 'pointer'
    return None


def internal(device, name):
    """The machine's own keyboard/trackpad: named like the dockchannel HID devices, or on the host/SPI bus. USB and
    Bluetooth devices are external and never keep the internal light on."""
    try:
        bus = int(read(device/'id/bustype', '0'), 16)
    except ValueError:
        bus = 0
    if bus in (BUS_USB, BUS_BLUETOOTH):
        return False
    return bool(INTERNAL_NAME.match(name)) or bus in (BUS_HOST, BUS_SPI)


def select_inputs(sys_input='/sys/class/input', dev_input='/dev/input'):
    """{/dev/input/eventN: (name, kind)} for the internal keyboards and trackpads."""
    found = {}
    for entry in sorted(glob.glob(os.path.join(sys_input, 'event[0-9]*'))):
        device = Path(entry)/'device'
        name = read(device/'name')
        kind = classify(device)
        if kind and internal(device, name):
            found[os.path.join(dev_input, os.path.basename(entry))] = (name, kind)
    return found


class Led:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.name = self.dir.name
        self.maximum = int(read(self.dir/'max_brightness', '0') or 0)

    def get(self):
        return int(read(self.dir/'brightness', '0'))

    def set(self, level):
        (self.dir/'brightness').write_text('%d\n' % level)


def find_led(leds='/sys/class/leds'):
    for entry in sorted(glob.glob(os.path.join(leds, '*kbd_backlight*'))):
        if os.path.exists(os.path.join(entry, 'brightness')):
            return Led(entry)
    return None


def slot_owners(led_name, run_user=RUN_USER):
    """UIDs with a brightnessctl saved slot for this LED. The desktop user is not always UID 1000 (a build account
    can own that), so the slot files themselves name the users whose `restore` would read a stale level."""
    owners = []
    for slot in sorted(glob.glob(os.path.join(run_user, '[0-9]*', 'brightnessctl', 'leds', glob.escape(led_name)))):
        uid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(slot))))
        if uid.isdigit() and int(uid) != 0:
            owners.append(int(uid))
    return owners


def owner_slot(led_name, level, run=subprocess.run, run_user=RUN_USER):
    """Refresh the owners' brightnessctl saved slots (best effort): Omarchy's lock blank is `brightnessctl -sd DEV set 0`
    and its unlock is `brightnessctl -rd DEV`; if the blank ran after our dim it saved 0, and the restore would put
    0 back. `-sd DEV set N` with the level already N saves N. It runs as each owner, never writing their files as root."""
    for uid in slot_owners(led_name, run_user):
        try:
            owner = pwd.getpwuid(uid).pw_name
        except KeyError:
            continue
        command = ['/usr/sbin/runuser', '-u', owner, '--', '/usr/bin/env',
                   'XDG_RUNTIME_DIR=%s' % os.path.join(run_user, str(uid)),
                   '/usr/bin/brightnessctl', '-q', '-sd', led_name, 'set', str(level)]
        try:
            run(command, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


class Idle:
    """The state machine; time, sysfs and the slot refresh are injected so tests run without hardware."""

    def __init__(self, led, now=time.monotonic, slot=owner_slot, saved_file=SAVED, opt_out=OPT_OUT, idle_s=IDLE_S):
        self.led, self.now, self.slot = led, now, slot
        self.saved_file, self.opt_out, self.idle_s = saved_file, opt_out, idle_s
        self.last_event = now()
        self.dimmed = None          # level we saved when we set 0, else None
        self.reassert_at = None
        self.restored_level = None
        self.log = []

    # persistence across a restart of this service
    def _persist(self, level):
        try:
            if level is None:
                os.unlink(self.saved_file)
            else:
                Path(self.saved_file).write_text('%d\n' % level)
        except OSError:
            pass

    def recover(self):
        """After a restart: a level saved by the previous instance that is still dimmed goes back."""
        text = read(self.saved_file)
        if not text.isdigit():
            return
        level = int(text)
        if self.led.get() == 0 and level > 0:
            self.led.set(level)
            self.log.append('recovered %d' % level)
        self._persist(None)

    def opted_out(self):
        return os.path.lexists(self.opt_out)

    def event(self):
        """Input activity. Returns a short note naming what happened."""
        self.last_event = self.now()
        self.reassert_at = None
        if self.dimmed is None:
            return 'activity'
        saved, self.dimmed = self.dimmed, None
        self._persist(None)
        if self.led.get() != 0:
            self.log.append('not restored: level is %d (changed by the owner or Omarchy)' % self.led.get())
            return 'left-alone'
        self.led.set(saved)
        self.slot(self.led.name, saved)
        self.restored_level = saved
        self.reassert_at = self.now()+REASSERT_S
        self.log.append('restored %d' % saved)
        return 'restored'

    def tick(self):
        """Time-driven part; call about once a second and after every select timeout."""
        now = self.now()
        if self.opted_out():
            if self.dimmed is not None:
                self.restore_for_exit()
            return 'opted-out'
        if self.reassert_at is not None and now >= self.reassert_at:
            self.reassert_at = None
            if self.dimmed is None and self.led.get() == 0 and self.restored_level:
                # something set 0 straight after our restore and no input followed: a resume handler racing us
                self.led.set(self.restored_level)
                self.log.append('re-asserted %d' % self.restored_level)
                return 're-asserted'
        if self.dimmed is None and now-self.last_event >= self.idle_s:
            level = self.led.get()
            if level > 0:
                self.dimmed = level
                self._persist(level)
                self.led.set(0)
                self.log.append('dimmed from %d' % level)
                return 'dimmed'
        return 'steady'

    def restore_for_exit(self):
        """Stop/opt-out: leave the light as the owner had it, so systemd-backlight saves the real level."""
        saved, self.dimmed = self.dimmed, None
        if saved is not None and self.led.get() == 0:
            self.led.set(saved)
            self.log.append('restored %d on exit' % saved)
        self._persist(None)


class Inputs:
    """Open event devices, rescanned every RESCAN_S (hot-plug)."""

    def __init__(self, selector, opener=lambda path: os.open(path, os.O_RDONLY | os.O_NONBLOCK)):
        self.selector, self.opener = selector, opener
        self.fds = {}      # fd -> path
        self.buffers = {}

    def rescan(self):
        wanted = self.selector()
        for fd, path in list(self.fds.items()):
            if path not in wanted:
                self.close(fd)
        have = set(self.fds.values())
        for path in wanted:
            if path in have:
                continue
            try:
                fd = self.opener(path)
            except OSError:
                continue
            self.fds[fd] = path
            self.buffers[fd] = b''

    def close(self, fd):
        self.fds.pop(fd, None)
        self.buffers.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def activity(self, fd):
        """Read what is pending on fd; True when any EV_KEY/EV_REL/EV_ABS record arrived."""
        try:
            data = os.read(fd, EVENT.size*64)
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno in (errno.ENODEV, errno.EBADF, errno.EIO):
                self.close(fd)
            return False
        if not data:
            self.close(fd)
            return False
        data = self.buffers.get(fd, b'')+data
        usable = len(data)-len(data) % EVENT.size
        self.buffers[fd] = data[usable:]
        for offset in range(0, usable, EVENT.size):
            _s, _u, kind, _code, _value = EVENT.unpack_from(data, offset)
            if kind in ACTIVITY:
                return True
        return False


def main(argv, leds='/sys/class/leds', sys_input='/sys/class/input', dev_input='/dev/input', idle_s=IDLE_S):
    stop = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.append(signum))
    waited = 0.0
    led = find_led(leds)
    while led is None and waited < 30.0 and not stop:
        time.sleep(1.0)
        waited += 1.0
        led = find_led(leds)
    if led is None:
        print('m4-kbd-idle: no keyboard backlight LED (*kbd_backlight*); nothing to do', flush=True)
        return 0
    idle = Idle(led, idle_s=idle_s)
    idle.recover()
    inputs = Inputs(lambda: select_inputs(sys_input, dev_input))
    inputs.rescan()
    print('m4-kbd-idle: %s off after %gs idle; watching %s' % (
        led.name, idle_s, ', '.join(sorted(inputs.fds.values())) or 'nothing yet'), flush=True)
    next_scan = time.monotonic()+RESCAN_S
    reported = 0
    try:
        while not stop:
            fds = list(inputs.fds)
            ready = []
            if fds:
                try:
                    ready = select.select(fds, [], [], 1.0)[0]
                except (OSError, ValueError):
                    inputs.rescan()
                    continue
            else:
                time.sleep(1.0)
            active = False
            for fd in ready:
                active = inputs.activity(fd) or active
            try:
                if active:
                    idle.event()
                idle.tick()
            except OSError as error:
                print('m4-kbd-idle: LED access failed: %s' % error, flush=True)
            if time.monotonic() >= next_scan:
                inputs.rescan()
                next_scan = time.monotonic()+RESCAN_S
            while reported < len(idle.log):
                print('m4-kbd-idle: ' + idle.log[reported], flush=True)
                reported += 1
    finally:
        try:
            idle.restore_for_exit()
        except OSError:
            pass
        for line in idle.log[reported:]:
            print('m4-kbd-idle: ' + line, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
