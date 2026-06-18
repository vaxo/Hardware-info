#!/usr/bin/env python3
"""Privileged helper for HardScope fan/thermal control.

The GUI runs as a normal user and cannot write the root-owned ACPI
``platform_profile`` file, so it spawns one of these helpers via ``pkexec``,
authenticates once, then streams commands over a stdin pipe — a single polkit
prompt per session instead of one per click.

Why platform_profile and not dell_smm pwm? On many Dell laptops (incl. the
Precision 5510) the firmware rejects dell-smm-hwmon pwm writes with EINVAL —
the BIOS owns the fans. The ACPI platform profile (cool/quiet/balanced/
performance) is the lever the firmware *does* honour.

Protocol — one command per line on stdin, one reply per line on stdout:

    ready                 (printed once at startup, before any command)
    profile <name>        set platform_profile (must be an advertised choice)
    ping                  liveness check
    -> "ok" on success, "err: <reason>" otherwise
"""

import sys

PROFILE = "/sys/firmware/acpi/platform_profile"
CHOICES = "/sys/firmware/acpi/platform_profile_choices"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def main():
    choices = set(_read(CHOICES).split())
    if not choices:
        print("err: no platform_profile support", flush=True)
        return 1

    print("ready", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        cmd = parts[0]
        try:
            if cmd == "ping":
                print("ok", flush=True)
            elif cmd == "profile" and len(parts) == 2 and parts[1] in choices:
                with open(PROFILE, "w") as f:
                    f.write(parts[1])
                print("ok", flush=True)
            else:
                print("err: bad command", flush=True)
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            print(f"err: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
