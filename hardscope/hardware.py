"""HardScope hardware-reading engine.

All hardware data is gathered here and returned as plain dicts/lists so the UI
layer stays dumb. Everything degrades gracefully: a missing file, an absent
tool, or a permission error yields a "—" / empty result rather than a crash.

Data sources used on Fedora/Linux:
  * /proc/cpuinfo, /proc/meminfo, /proc/uptime   - kernel info
  * psutil                                        - live usage/freq/mem/disk
  * /sys/class/hwmon/*                            - temps, fans, voltages
  * /sys/class/dmi/id/*                           - motherboard / BIOS
  * /sys/class/drm/card*/device/*                 - GPU clocks/usage/vendor
  * lspci / lsblk / smartctl (subprocess)         - PCI names, disks, SMART
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess
import time

import psutil

HWMON = "/sys/class/hwmon"
DMI = "/sys/class/dmi/id"
DRM = "/sys/class/drm"

# PCI vendor IDs we care about for GPUs.
GPU_VENDORS = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD"}

# Friendly names for common (subsystem) PCI vendors.
PCI_VENDOR_NAMES = {
    "0x1028": "Dell", "0x10de": "NVIDIA", "0x8086": "Intel", "0x1002": "AMD/ATI",
    "0x1043": "ASUS", "0x1458": "Gigabyte", "0x1462": "MSI", "0x17aa": "Lenovo",
    "0x103c": "HP", "0x1025": "Acer", "0x144d": "Samsung", "0x1414": "Microsoft",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _read(path, default=None):
    """Read and strip a sysfs/proc file, returning default on any error."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _read_int(path, default=None):
    val = _read(path)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _run(cmd, timeout=4):
    """Run a command, returning stdout or "" on any failure/timeout."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _fmt_bytes(n):
    """Human-readable size (binary units)."""
    if n is None:
        return "—"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{f:.1f} PiB"


# --------------------------------------------------------------------------
# sensors (hwmon) — shared by CPU/GPU/sensors pages
# --------------------------------------------------------------------------
def read_hwmon():
    """Return a list of hwmon chips, each with temps/fans/voltages.

    [{name, path, temps:[{label,value_c}], fans:[{label,rpm}],
      volts:[{label,v}]}]
    """
    chips = []
    for chip_path in sorted(glob.glob(f"{HWMON}/hwmon*")):
        name = _read(os.path.join(chip_path, "name")) or os.path.basename(chip_path)
        temps, fans, volts = [], [], []

        for tin in sorted(glob.glob(os.path.join(chip_path, "temp*_input"))):
            idx = re.search(r"temp(\d+)_input", tin).group(1)
            raw = _read_int(tin)
            if raw is None:
                continue
            label = _read(os.path.join(chip_path, f"temp{idx}_label")) or f"temp{idx}"
            temps.append({"label": label, "value_c": raw / 1000.0})

        for fin in sorted(glob.glob(os.path.join(chip_path, "fan*_input"))):
            idx = re.search(r"fan(\d+)_input", fin).group(1)
            raw = _read_int(fin)
            if raw is None:
                continue
            label = _read(os.path.join(chip_path, f"fan{idx}_label")) or f"fan{idx}"
            fans.append({"label": label, "rpm": raw})

        for vin in sorted(glob.glob(os.path.join(chip_path, "in*_input"))):
            idx = re.search(r"in(\d+)_input", vin).group(1)
            raw = _read_int(vin)
            if raw is None:
                continue
            label = _read(os.path.join(chip_path, f"in{idx}_label")) or f"in{idx}"
            volts.append({"label": label, "v": raw / 1000.0})

        if temps or fans or volts:
            chips.append(
                {
                    "name": name,
                    "path": chip_path,
                    "temps": temps,
                    "fans": fans,
                    "volts": volts,
                }
            )
    return chips


def _hwmon_by_name(chips, *names):
    """First hwmon chip whose name matches one of `names`."""
    for c in chips:
        if c["name"] in names:
            return c
    return None


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------
def read_cpu(chips=None):
    if chips is None:
        chips = read_hwmon()

    model = "Unknown CPU"
    vendor = "—"
    cpuinfo = _read("/proc/cpuinfo") or ""
    for line in cpuinfo.splitlines():
        if line.startswith("model name") and model == "Unknown CPU":
            model = line.split(":", 1)[1].strip()
        elif line.startswith("vendor_id") and vendor == "—":
            vendor = line.split(":", 1)[1].strip()

    freq = psutil.cpu_freq()
    per_core_freq = psutil.cpu_freq(percpu=True)
    per_core_usage = psutil.cpu_percent(percpu=True)  # since last call

    # CPU temperature: prefer coretemp package, else acpitz.
    pkg_temp = None
    core_temps = []
    coretemp = _hwmon_by_name(chips, "coretemp", "k10temp", "zenpower")
    if coretemp:
        for t in coretemp["temps"]:
            if "Package" in t["label"] or "Tctl" in t["label"] or "Tdie" in t["label"]:
                pkg_temp = t["value_c"]
            else:
                core_temps.append(t)
        if pkg_temp is None and coretemp["temps"]:
            pkg_temp = max(t["value_c"] for t in coretemp["temps"])
    if pkg_temp is None:
        acpi = _hwmon_by_name(chips, "acpitz")
        if acpi and acpi["temps"]:
            pkg_temp = acpi["temps"][0]["value_c"]

    return {
        "model": model,
        "vendor": vendor,
        "arch": platform.machine(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "freq_current": freq.current if freq else None,
        "freq_min": freq.min if freq else None,
        "freq_max": freq.max if freq else None,
        "usage_total": psutil.cpu_percent(),
        "per_core_usage": per_core_usage,
        "per_core_freq": [f.current for f in per_core_freq] if per_core_freq else [],
        "package_temp": pkg_temp,
        "core_temps": core_temps,
        "load_avg": os.getloadavg(),
    }


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------
def read_memory(include_modules=True):
    """Memory usage (cheap). Physical module list (slow, needs root via
    dmidecode) is only collected when include_modules is True."""
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    # Physical modules need dmidecode (root). Try, but don't require it.
    modules = []
    if include_modules and shutil.which("dmidecode"):
        out = _run(["dmidecode", "-t", "memory"])
        block = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Memory Device"):
                if block.get("size") and "No Module" not in block.get("size", ""):
                    modules.append(block)
                block = {}
            elif ":" in line:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                if k == "Size":
                    block["size"] = v
                elif k == "Type":
                    block["type"] = v
                elif k == "Speed":
                    block["speed"] = v
                elif k == "Manufacturer":
                    block["mfr"] = v
                elif k == "Locator":
                    block["slot"] = v
        if block.get("size") and "No Module" not in block.get("size", ""):
            modules.append(block)

    return {
        "total": vm.total,
        "used": vm.used,
        "available": vm.available,
        "percent": vm.percent,
        "swap_total": sm.total,
        "swap_used": sm.used,
        "swap_percent": sm.percent,
        "modules": modules,
        "needs_root": include_modules and not modules and shutil.which("dmidecode") is not None,
    }


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------
def list_gpu_pci():
    """Map short PCI slot (e.g. '01:00.0') -> {name, cls} from lspci."""
    lspci = _run(["lspci", "-mm"])
    out = {}
    for line in lspci.splitlines():
        m = re.search(r'"(VGA compatible controller|3D controller|Display controller)"', line)
        if not m:
            continue
        fields = re.findall(r'"([^"]*)"', line)
        slot = line.split()[0]
        vend = fields[1] if len(fields) > 1 else "?"
        dev = fields[2] if len(fields) > 2 else "?"
        out[slot] = {"name": f"{vend} {dev}", "cls": m.group(1)}
    return out


def read_opengl():
    """OpenGL renderer/version + reported video memory per GPU offload sink.

    Runs glxinfo for the default GPU and (best-effort, wakes a discrete GPU
    once) DRI_PRIME=1 for the offload GPU. Keyed by vendor: 'Intel'/'NVIDIA'/'AMD'.
    Slow — call once from read_static(), never on the tick.
    """
    if not shutil.which("glxinfo"):
        return {}

    def _parse(env=None):
        cmd_env = dict(os.environ)
        if env:
            cmd_env.update(env)
        try:
            out = subprocess.run(
                ["glxinfo", "-B"], capture_output=True, text=True,
                timeout=25, check=False, env=cmd_env,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        info = {}
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("OpenGL renderer string:"):
                info["renderer"] = s.split(":", 1)[1].strip()
            elif s.startswith("OpenGL core profile version string:"):
                info["gl_version"] = s.split(":", 1)[1].strip()
            elif s.startswith("OpenGL version string:") and "gl_version" not in info:
                info["gl_version"] = s.split(":", 1)[1].strip()
            elif s.startswith("Dedicated video memory:"):
                info["vram"] = s.split(":", 1)[1].strip()
            elif s.startswith("Video memory:") and "vram" not in info:
                info["vram"] = s.split(":", 1)[1].strip()
        return info or None

    results = {}
    for env in (None, {"DRI_PRIME": "1"}):
        info = _parse(env)
        if not info:
            continue
        r = info.get("renderer", "").lower()
        if "nvidia" in r or r.startswith("nv") or "geforce" in r or "quadro" in r:
            vendor = "NVIDIA"
        elif "amd" in r or "radeon" in r or "ati" in r:
            vendor = "AMD"
        elif "intel" in r or "mesa intel" in r:
            vendor = "Intel"
        else:
            vendor = info.get("renderer", "?")
        results.setdefault(vendor, info)
    return results


def read_displays():
    """Connected display outputs across all cards."""
    displays = []
    for con in sorted(glob.glob(f"{DRM}/card*-*")):
        if _read(os.path.join(con, "status")) != "connected":
            continue
        base = os.path.basename(con)              # e.g. card2-eDP-1
        card = base.split("-", 1)[0]
        connector = base.split("-", 1)[1]
        modes = _read(os.path.join(con, "modes")) or ""
        res = modes.splitlines()[0] if modes else "—"
        displays.append(
            {
                "card": card,
                "connector": connector,
                "resolution": res,
                "enabled": _read(os.path.join(con, "enabled")) or "—",
            }
        )
    return displays


def _gpu_device_sensor(dev):
    """(temp_c, voltage_v) from a GPU's own hwmon (nouveau/amdgpu), or (None, None)."""
    temp = volt = None
    for h in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
        t = _read_int(os.path.join(h, "temp1_input"))
        if t is not None:
            temp = t / 1000.0
        v = _read_int(os.path.join(h, "in0_input"))
        if v is not None:
            volt = v / 1000.0
    return temp, volt


def read_gpus(chips=None, pci_map=None, opengl=None, cpu_pkg_temp=None):
    if chips is None:
        chips = read_hwmon()
    if pci_map is None:
        pci_map = list_gpu_pci()

    gpus = []
    for card in sorted(glob.glob(f"{DRM}/card*")):
        if re.search(r"card\d+-", card):  # skip connector dirs like card2-DP-1
            continue
        cardname = os.path.basename(card)
        dev = os.path.join(card, "device")
        vendor_id = _read(os.path.join(dev, "vendor"))
        if not vendor_id:
            continue
        vendor = GPU_VENDORS.get(vendor_id, vendor_id)

        # PCI identity
        pci_path = os.path.realpath(dev)                  # /sys/.../0000:01:00.0
        slot_full = os.path.basename(pci_path)            # 0000:01:00.0
        slot_short = slot_full.split(":", 1)[1] if ":" in slot_full else slot_full
        pinfo = pci_map.get(slot_short, {})
        name = pinfo.get("name") or f"{vendor} GPU"
        device_id = _read(os.path.join(dev, "device"))
        revision = _read(os.path.join(dev, "revision"))
        subv = _read(os.path.join(dev, "subsystem_vendor"))
        subd = _read(os.path.join(dev, "subsystem_device"))
        subsystem = PCI_VENDOR_NAMES.get(subv, subv or "—")
        if subd:
            subsystem = f"{subsystem} [{(subv or '')[2:]}:{subd[2:]}]"
        driver = os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) \
            if os.path.exists(os.path.join(dev, "driver")) else "—"

        # Power management
        runtime = _read(os.path.join(dev, "power/runtime_status")) or "—"
        power_state = _read(os.path.join(dev, "power_state")) or "—"

        # Temperature / voltage from the card's own hwmon (nouveau/amdgpu).
        temp, volt = _gpu_device_sensor(dev)
        temp_note = ""
        if temp is None and vendor == "Intel" and cpu_pkg_temp is not None:
            temp = cpu_pkg_temp
            temp_note = "shared with CPU package"

        # Clocks: Intel exposes gt_*_freq_mhz directly under the card.
        clk_cur = _read_int(os.path.join(card, "gt_act_freq_mhz")) \
            or _read_int(os.path.join(card, "gt_cur_freq_mhz"))
        clk_min = _read_int(os.path.join(card, "gt_min_freq_mhz")) \
            or _read_int(os.path.join(card, "gt_RPn_freq_mhz"))
        clk_max = _read_int(os.path.join(card, "gt_max_freq_mhz")) \
            or _read_int(os.path.join(card, "gt_RP0_freq_mhz"))

        # Utilisation + VRAM (amdgpu only via sysfs).
        busy = _read_int(os.path.join(dev, "gpu_busy_percent"))
        vram_total = _read_int(os.path.join(dev, "mem_info_vram_total"))
        vram_used = _read_int(os.path.join(dev, "mem_info_vram_used"))

        gl = (opengl or {}).get(vendor, {})

        gpus.append(
            {
                "card": cardname,
                "vendor": vendor,
                "name": name,
                "pci_class": pinfo.get("cls", "—"),
                "pci_slot": slot_full,
                "pci_id": f"{(vendor_id or '')[2:]}:{(device_id or '')[2:]}",
                "revision": revision or "—",
                "subsystem": subsystem,
                "driver": driver,
                "runtime_status": runtime,
                "power_state": power_state,
                "temp_c": temp,
                "temp_note": temp_note,
                "voltage_v": volt,
                "clock_cur": clk_cur,
                "clock_min": clk_min,
                "clock_max": clk_max,
                "busy_percent": busy,
                "vram_total": vram_total,
                "vram_used": vram_used,
                "gl_renderer": gl.get("renderer"),
                "gl_version": gl.get("gl_version"),
                "gl_vram": gl.get("vram"),
            }
        )
    return gpus


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def disk_temp(chips):
    """Live NVMe drive temperature from the nvme hwmon chip, or None."""
    nvme = _hwmon_by_name(chips, "nvme")
    if nvme and nvme["temps"]:
        return nvme["temps"][0]["value_c"]
    return None


def read_storage(chips=None, include_disks=True):
    """Disk inventory + filesystem usage. When include_disks is False the
    slow part (lsblk + per-drive smartctl) is skipped — partitions only."""
    if chips is None:
        chips = read_hwmon()

    disks = []
    nvme_temp = disk_temp(chips)
    if not include_disks:
        return {"disks": [], "partitions": _read_partitions()}

    out = _run(["lsblk", "-d", "-b", "-o", "NAME,MODEL,SIZE,ROTA,TRAN,SERIAL", "-J"])

    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {}

    for d in data.get("blockdevices", []):
        name = d.get("name", "")
        if name.startswith(("loop", "ram", "zram", "sr")):
            continue
        dev_path = f"/dev/{name}"
        rota = d.get("rota")
        kind = "HDD" if rota in (True, "1", 1) else "SSD"
        temp = nvme_temp if name.startswith("nvme") else None

        # SMART health — best-effort, needs root for most drives.
        health = "—"
        if shutil.which("smartctl"):
            sout = _run(["smartctl", "-H", dev_path], timeout=6)
            m = re.search(r"(?:overall-health self-assessment test result:|SMART Health Status:)\s*([A-Za-z]+)", sout)
            if m:
                health = m.group(1).upper()
            elif "Permission denied" in sout or "requires root" in sout.lower():
                health = "needs root"

        disks.append(
            {
                "name": name,
                "model": (d.get("model") or "").strip() or "—",
                "size": d.get("size"),
                "type": kind,
                "bus": (d.get("tran") or "").upper() or "—",
                "serial": (d.get("serial") or "").strip() or "—",
                "temp_c": temp,
                "health": health,
            }
        )

    return {"disks": disks, "partitions": _read_partitions()}


def _read_partitions():
    """Mounted filesystem usage (cheap, psutil only)."""
    parts = []
    for p in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        parts.append(
            {
                "device": p.device,
                "mount": p.mountpoint,
                "fstype": p.fstype,
                "total": u.total,
                "used": u.used,
                "percent": u.percent,
            }
        )
    return parts


# --------------------------------------------------------------------------
# System / motherboard / OS
# --------------------------------------------------------------------------
def read_system():
    osrel = {}
    for line in (_read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        osrel[k] = v.strip().strip('"')

    uptime_s = None
    up = _read("/proc/uptime")
    if up:
        try:
            uptime_s = float(up.split()[0])
        except (ValueError, IndexError):
            pass

    uname = platform.uname()
    return {
        "hostname": uname.node,
        "os_name": osrel.get("PRETTY_NAME", "Linux"),
        "kernel": uname.release,
        "arch": uname.machine,
        "uptime_s": uptime_s,
        "boot_time": psutil.boot_time(),
        # motherboard / BIOS
        "sys_vendor": _read(f"{DMI}/sys_vendor") or "—",
        "product": _read(f"{DMI}/product_name") or "—",
        "board_vendor": _read(f"{DMI}/board_vendor") or "—",
        "board_name": _read(f"{DMI}/board_name") or "—",
        "bios_vendor": _read(f"{DMI}/bios_vendor") or "—",
        "bios_version": _read(f"{DMI}/bios_version") or "—",
        "bios_date": _read(f"{DMI}/bios_date") or "—",
        "chassis": _read(f"{DMI}/chassis_type") or "—",
    }


def read_network():
    nets = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    io = psutil.net_io_counters(pernic=True)
    for name, st in stats.items():
        ipv4 = ipv6 = mac = "—"
        for a in addrs.get(name, []):
            if a.family.name == "AF_INET":
                ipv4 = a.address
            elif a.family.name == "AF_INET6":
                ipv6 = a.address.split("%")[0]
            elif a.family.name == "AF_PACKET":
                mac = a.address
        counters = io.get(name)
        nets.append(
            {
                "name": name,
                "up": st.isup,
                "speed_mbps": st.speed or 0,
                "mac": mac,
                "ipv4": ipv4,
                "ipv6": ipv6,
                "sent": counters.bytes_sent if counters else 0,
                "recv": counters.bytes_recv if counters else 0,
            }
        )
    return sorted(nets, key=lambda n: (not n["up"], n["name"]))


def uptime_str(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


# --------------------------------------------------------------------------
# aggregators used by the UI
# --------------------------------------------------------------------------
def read_static():
    """Slow, rarely-changing data: gathered once at startup.

    Spawns dmidecode / lspci / lsblk / smartctl, so NOT for the refresh tick.
    """
    chips = read_hwmon()
    pci_map = list_gpu_pci()
    opengl = read_opengl()
    return {
        "system": read_system(),
        "memory_modules": read_memory(include_modules=True)["modules"],
        "gpus": read_gpus(chips, pci_map, opengl),
        "displays": read_displays(),
        "storage": read_storage(chips, include_disks=True),
        "pci_map": pci_map,
        "opengl": opengl,
    }


def read_dynamic(pci_map=None, opengl=None):
    """Cheap live data for the refresh tick: sysfs + psutil only, no
    subprocess spawning."""
    chips = read_hwmon()
    cpu = read_cpu(chips)
    return {
        "chips": chips,
        "cpu": cpu,
        "memory": read_memory(include_modules=False),
        "gpus": read_gpus(chips, pci_map or {}, opengl or {}, cpu.get("package_temp")),
        "disk_temp": disk_temp(chips),
        "partitions": _read_partitions(),
        "network": read_network(),
        "system_live": {
            "uptime_s": float((_read("/proc/uptime") or "0").split()[0] or 0),
        },
    }


# expose formatter for the UI
fmt_bytes = _fmt_bytes


if __name__ == "__main__":
    # quick smoke test: dump everything as JSON
    chips = read_hwmon()
    snapshot = {
        "system": read_system(),
        "cpu": read_cpu(chips),
        "memory": read_memory(),
        "gpus": read_gpus(chips),
        "storage": read_storage(chips),
        "network": read_network(),
        "sensors": chips,
    }
    print(json.dumps(snapshot, indent=2, default=str))
