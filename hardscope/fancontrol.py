"""Laptop fan / thermal control via the ACPI platform profile.

On this hardware (and most modern Dells) the BIOS owns the fans: dell-smm-hwmon
pwm writes are rejected with EINVAL, so exact RPM or per-fan duty cannot be set
from software. What *does* work is the ACPI ``platform_profile`` — a small set
of firmware thermal profiles (cool/quiet/balanced/performance) that change how
aggressively the BIOS spins the fans. That is what this module drives.

Two layers, mirroring the rest of HardScope:

* Reading (``available`` / ``read_profile`` / ``read_fans``) touches only
  world-readable sysfs and needs no privileges — safe to call every tick.
* Writing the profile is root-only, so :class:`FanController` drives the
  privileged ``_fanhelper`` over a pipe (launched once via ``pkexec``).
"""

from __future__ import annotations

import atexit
import glob
import os
import subprocess
import sys

HWMON = "/sys/class/hwmon"
PROFILE = "/sys/firmware/acpi/platform_profile"
CHOICES = "/sys/firmware/acpi/platform_profile_choices"

# Friendly one-line description per profile, shown under each button.
PROFILE_HINTS = {
    "cool": "Coolest surface, fans favour low temps",
    "quiet": "Minimal fan noise; fans may stop when idle",
    "balanced": "Default trade-off of noise and cooling",
    "performance": "Highest fan ceiling for sustained load",
}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return None


def available():
    """True if this machine exposes a writable ACPI platform profile."""
    return os.path.exists(PROFILE) and bool(_read(CHOICES))


def read_profile():
    """{'current': str|None, 'choices': [str, ...]} for the platform profile."""
    return {
        "current": _read(PROFILE),
        "choices": (_read(CHOICES) or "").split(),
    }


def _dell_chip():
    for path in sorted(glob.glob(f"{HWMON}/hwmon*")):
        if _read(os.path.join(path, "name")) == "dell_smm":
            return path
    return None


def read_fans():
    """Live read-only fan state: [{idx, label, rpm, max}]. Empty if none."""
    chip = _dell_chip()
    fans = []
    if not chip:
        return fans
    for fin in sorted(glob.glob(os.path.join(chip, "fan*_input"))):
        idx = os.path.basename(fin)[3:-6]  # fan<idx>_input
        fans.append(
            {
                "idx": idx,
                "label": _read(os.path.join(chip, f"fan{idx}_label")) or f"Fan {idx}",
                "rpm": _read_int(fin),
                "max": _read_int(os.path.join(chip, f"fan{idx}_max")) or 0,
            }
        )
    return fans


class FanController:
    """Owns the privileged helper subprocess; one polkit prompt per session."""

    def __init__(self):
        self._proc = None
        atexit.register(self.shutdown)

    def _ensure(self):
        """Start the helper if needed. Returns True once it is serving.

        The first call blocks on the polkit password dialog; callers should
        treat it as a user-initiated, possibly slow action.
        """
        if self._proc and self._proc.poll() is None:
            return True
        helper = os.path.join(os.path.dirname(__file__), "_fanhelper.py")
        try:
            self._proc = subprocess.Popen(
                ["pkexec", sys.executable, helper],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError):
            self._proc = None
            return False
        # "ready" means we got privileges; EOF/anything else = auth cancelled.
        if self._proc.stdout.readline().strip() != "ready":
            self.shutdown()
            return False
        return True

    def _send(self, command):
        if not self._ensure():
            return False
        try:
            self._proc.stdin.write(command + "\n")
            self._proc.stdin.flush()
            return self._proc.stdout.readline().strip() == "ok"
        except (BrokenPipeError, OSError):
            self.shutdown()
            return False

    def active(self):
        return bool(self._proc and self._proc.poll() is None)

    def set_profile(self, name):
        """Switch the firmware thermal profile (e.g. 'quiet', 'performance')."""
        return self._send(f"profile {name}")

    def shutdown(self):
        proc, self._proc = self._proc, None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()
