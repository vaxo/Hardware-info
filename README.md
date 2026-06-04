# HardScope

A [Speccy](https://www.ccleaner.com/speccy)-style hardware monitor for Linux —
built natively for Fedora with GTK4 / libadwaita. It shows your CPU,
motherboard, memory, graphics, storage and live sensor readings (temperatures,
fan speeds, voltages) in a clean sidebar layout.

![layout](docs/layout.txt)

## Features

- **Summary** — one-glance overview of the whole machine
- **CPU** — model, cores/threads, live per-core usage + frequency, package temp
- **Memory** — usage, swap, and physical DIMM details (with `sudo`)
- **Graphics** — each GPU's model, temperature, utilisation, VRAM
- **Storage** — drives (model/size/bus/SMART health), NVMe temp, filesystem usage
- **Sensors** — every `hwmon` chip: temperatures, fans (RPM), voltages — live
- **Network** — interfaces, IP/MAC, link speed, traffic counters
- **System** — OS, kernel, motherboard, BIOS, uptime

Live values refresh every 1.5 s. All readings come straight from the kernel
(`/proc`, `/sys/class/hwmon`, `/sys/class/dmi`) plus `psutil`, so it works
without any daemon.

## Requirements

The whole stack ships with a default Fedora 44 Workstation. If anything is
missing:

```bash
sudo dnf install python3-gobject gtk4 libadwaita python3-psutil \
                 pciutils util-linux dmidecode smartmontools
```

## Run

```bash
cd hardscope
python3 -m hardscope        # or:  ./run.py
```

Some details need root and otherwise show as `needs root` / `—`:

- **DIMM details** (manufacturer, speed, slot) via `dmidecode`
- **SMART health** of SATA drives via `smartctl`

To see them, launch with privileges:

```bash
sudo python3 -m hardscope
```

## Headless / data-only

The engine runs standalone and dumps everything as JSON — handy for scripting
or debugging:

```bash
python3 -m hardscope.hardware | jq .
```

## Install a desktop launcher

```bash
cp io.github.hardscope.HardScope.desktop ~/.local/share/applications/
# edit Exec= to an absolute path if you didn't put hardscope on PYTHONPATH
```

## Architecture

| File | Role |
|------|------|
| `hardscope/hardware.py` | Reading engine — all kernel/sysfs/tool access. No UI. Split into `read_static()` (slow, once) and `read_dynamic()` (cheap, per tick). |
| `hardscope/window.py`   | GTK4 window: sidebar + per-category panels, live refresh timer. |
| `hardscope/__main__.py` | `Adw.Application` entry point. |

The UI never touches sysfs directly — it only consumes the dicts the engine
returns, so the two layers can evolve independently.

## Roadmap ideas

- Live history graphs (temperature / usage sparklines)
- Export a full report to text/HTML (a signature Speccy feature)
- System-tray temperature indicator
- NVIDIA proprietary-driver support via NVML (currently uses `nouveau` hwmon)
- Per-process resource view
