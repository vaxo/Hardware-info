"""HardScope GTK4 / libadwaita UI.

A sidebar of categories on the left, a scrollable detail panel on the right —
the familiar Speccy layout. Static data (hardware inventory) is read once at
startup; live values (temps, usage, fans, clocks) refresh on a timer.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import hardware as hw  # noqa: E402

REFRESH_MS = 1500


def _temp(c):
    return f"{c:.0f} °C" if isinstance(c, (int, float)) else "—"


def _pct(p):
    return f"{p:.0f} %" if isinstance(p, (int, float)) else "—"


def _mhz(m):
    return f"{m/1000:.2f} GHz" if isinstance(m, (int, float)) and m else "—"


class StatGroup(Gtk.Box):
    """A titled group of key/value rows; values can be updated live by key."""

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
        self.append(self._group)
        self._labels = {}

    def add(self, key, title, value="—"):
        row = Adw.ActionRow(title=GLib.markup_escape_text(str(title)))
        lbl = Gtk.Label(label=str(value))
        lbl.add_css_class("dim-label")
        lbl.set_selectable(True)
        lbl.set_xalign(1.0)
        row.add_suffix(lbl)
        self._group.add(row)
        self._labels[key] = lbl
        return row

    def add_bar(self, key, title):
        """A row with a usage LevelBar + percentage label."""
        row = Adw.ActionRow(title=GLib.markup_escape_text(str(title)))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar = Gtk.LevelBar(min_value=0, max_value=100)
        bar.set_size_request(160, -1)
        bar.set_valign(Gtk.Align.CENTER)
        lbl = Gtk.Label(label="—")
        lbl.add_css_class("dim-label")
        lbl.set_width_chars(11)
        lbl.set_xalign(1.0)
        box.append(bar)
        box.append(lbl)
        row.add_suffix(box)
        self._group.add(row)
        self._labels[key] = (bar, lbl)
        return row

    def set(self, key, value):
        lbl = self._labels.get(key)
        if isinstance(lbl, Gtk.Label):
            lbl.set_label(str(value))

    def set_bar(self, key, percent, text=None):
        entry = self._labels.get(key)
        if isinstance(entry, tuple):
            bar, lbl = entry
            bar.set_value(max(0, min(100, percent or 0)))
            lbl.set_label(text if text is not None else _pct(percent))


def _page(*groups):
    """Wrap stat groups in a scrollable, clamped column."""
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    col.set_margin_top(18)
    col.set_margin_bottom(18)
    col.set_margin_start(12)
    col.set_margin_end(12)
    for g in groups:
        col.append(g)
    clamp = Adw.Clamp(maximum_size=820, child=col)
    scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    scroller.set_child(clamp)
    return scroller


class HardScopeWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="HardScope")
        self.set_default_size(900, 640)

        self.static = hw.read_static()
        self._pci = self.static["pci_map"]
        self._opengl = self.static.get("opengl", {})
        self._core_bars = []
        self._sensor_labels = {}  # (chip, kind, label) -> Gtk.Label
        self._updaters = []  # list of fn(dyn)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(stack)
        sidebar.set_size_request(180, -1)

        stack.add_titled(self._build_summary(), "summary", "Summary")
        stack.add_titled(self._build_cpu(), "cpu", "CPU")
        stack.add_titled(self._build_memory(), "memory", "Memory")
        stack.add_titled(self._build_graphics(), "graphics", "Graphics")
        stack.add_titled(self._build_storage(), "storage", "Storage")
        stack.add_titled(self._build_sensors(), "sensors", "Sensors")
        stack.add_titled(self._build_network(), "network", "Network")
        stack.add_titled(self._build_system(), "system", "System")

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        split.append(sidebar)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(stack)

        header = Adw.HeaderBar()
        self._subtitle = Gtk.Label(label="reading…")
        self._subtitle.add_css_class("dim-label")
        header.pack_end(self._subtitle)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(split)
        self.set_content(toolbar)

        self._tick()  # paint live values immediately
        GLib.timeout_add(REFRESH_MS, self._tick)

    # ---- page builders --------------------------------------------------
    def _build_summary(self):
        sysd = self.static["system"]
        g = StatGroup("System")
        g.add("os", "Operating System", sysd["os_name"])
        g.add("host", "Computer", f"{sysd['sys_vendor']} {sysd['product']}")
        g.add("uptime", "Uptime")

        cpu = StatGroup("CPU")
        cpu.add("model", "Model", self.static_cpu_model())
        cpu.add("temp", "Temperature")
        cpu.add("usage", "Usage")

        misc = StatGroup("Memory & Graphics")
        misc.add("ram", "RAM")
        gnames = ", ".join(self._short(g["name"]) for g in self.static["gpus"]) or "—"
        misc.add("gpu", "Graphics", gnames)
        misc.add("gputemp", "GPU Temperature")

        def update(dyn):
            g.set("uptime", hw.uptime_str(dyn["system_live"]["uptime_s"]))
            cpu.set("temp", _temp(dyn["cpu"]["package_temp"]))
            cpu.set("usage", _pct(dyn["cpu"]["usage_total"]))
            m = dyn["memory"]
            misc.set("ram", f"{hw.fmt_bytes(m['used'])} / {hw.fmt_bytes(m['total'])} ({_pct(m['percent'])})")
            gtemps = [_temp(x["temp_c"]) for x in dyn["gpus"] if x["temp_c"] is not None]
            misc.set("gputemp", ", ".join(gtemps) if gtemps else "—")

        self._updaters.append(update)
        return _page(g, cpu, misc)

    def _build_cpu(self):
        info = StatGroup("Processor")
        c = self.static_cpu()
        info.add("model", "Model", c["model"])
        info.add("vendor", "Vendor", c["vendor"])
        info.add("arch", "Architecture", c["arch"])
        info.add("cores", "Cores / Threads", f"{c['physical_cores']} / {c['logical_cores']}")
        info.add("maxfreq", "Max Frequency", _mhz(c["freq_max"]))
        info.add("curfreq", "Current Frequency")
        info.add("temp", "Package Temperature")
        info.add("load", "Load Average (1/5/15m)")

        usage = StatGroup("Per-Core Usage")
        self._core_bars = []
        for i in range(c["logical_cores"] or 0):
            usage.add_bar(f"core{i}", f"Core {i}")
            self._core_bars.append(f"core{i}")

        def update(dyn):
            cc = dyn["cpu"]
            info.set("curfreq", _mhz(cc["freq_current"]))
            info.set("temp", _temp(cc["package_temp"]))
            la = cc["load_avg"]
            info.set("load", f"{la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}")
            for i, key in enumerate(self._core_bars):
                pct = cc["per_core_usage"][i] if i < len(cc["per_core_usage"]) else 0
                fr = cc["per_core_freq"][i] if i < len(cc["per_core_freq"]) else None
                txt = f"{pct:.0f}%  {fr/1000:.1f}GHz" if fr else f"{pct:.0f}%"
                usage.set_bar(key, pct, txt)

        self._updaters.append(update)
        return _page(info, usage)

    def _build_memory(self):
        g = StatGroup("Physical Memory")
        g.add_bar("used", "Used")
        g.add("total", "Total")
        g.add("avail", "Available")
        swap = StatGroup("Swap")
        swap.add_bar("swap", "Used")
        swap.add("swaptotal", "Total")

        groups = [g, swap]
        mods = self.static["memory_modules"]
        if mods:
            mg = StatGroup("Installed Modules")
            for i, m in enumerate(mods):
                mg.add(f"m{i}", m.get("slot", f"DIMM {i}"),
                       f"{m.get('size','?')} {m.get('type','')} {m.get('speed','')} · {m.get('mfr','')}".strip())
            groups.append(mg)
        else:
            note = StatGroup("Installed Modules")
            note.add("hint", "Per-stick details",
                     "run with sudo for dmidecode")
            groups.append(note)

        def update(dyn):
            m = dyn["memory"]
            g.set_bar("used", m["percent"],
                      f"{hw.fmt_bytes(m['used'])} ({_pct(m['percent'])})")
            g.set("total", hw.fmt_bytes(m["total"]))
            g.set("avail", hw.fmt_bytes(m["available"]))
            swap.set_bar("swap", m["swap_percent"],
                         f"{hw.fmt_bytes(m['swap_used'])} ({_pct(m['swap_percent'])})")
            swap.set("swaptotal", hw.fmt_bytes(m["swap_total"]))

        self._updaters.append(update)
        return _page(*groups)

    def _build_graphics(self):
        groups = []
        self._gpu_groups = []
        for idx, gpu in enumerate(self.static["gpus"]):
            g = StatGroup(f"GPU {idx} — {gpu['vendor']}")
            # --- identity (static) ---
            g.add("name", "Model", gpu["name"])
            g.add("driver", "Driver", gpu["driver"])
            g.add("class", "Type", gpu["pci_class"])
            g.add("pciid", "PCI ID", gpu["pci_id"])
            g.add("slot", "PCI Slot", gpu["pci_slot"])
            g.add("rev", "Revision", gpu["revision"])
            g.add("subsys", "Subsystem", gpu["subsystem"])
            # --- live state ---
            g.add("power", "Power State")
            g.add("temp", "Temperature")
            g.add("volt", "GPU Voltage")
            g.add("clock", "Clock (cur / max)")
            g.add("busy", "Utilisation")
            # --- memory / capabilities (static) ---
            vram_txt = gpu.get("gl_vram") or "—"
            if gpu["vendor"] == "Intel" and gpu.get("gl_vram"):
                vram_txt += "  (shared)"
            elif gpu.get("gl_vram"):
                vram_txt += "  (dedicated)"
            g.add("vram", "Video Memory", vram_txt)
            if gpu.get("gl_renderer"):
                g.add("glr", "OpenGL Renderer", gpu["gl_renderer"])
            if gpu.get("gl_version"):
                g.add("glv", "OpenGL Version", gpu["gl_version"])
            groups.append(g)
            self._gpu_groups.append(g)

        # Connected displays
        displays = self.static.get("displays", [])
        if displays:
            dg = StatGroup("Displays")
            for d in displays:
                dg.add(d["connector"],
                       f"{d['connector']}  ({d['card']})",
                       f"{d['resolution']} · {d['enabled']}")
            groups.append(dg)

        def update(dyn):
            for g, gpu in zip(self._gpu_groups, dyn["gpus"]):
                rt = gpu["runtime_status"]
                ps = gpu["power_state"]
                g.set("power", f"{rt} ({ps})" if ps != "—" else rt)

                if gpu["temp_c"] is not None:
                    t = _temp(gpu["temp_c"])
                    if gpu.get("temp_note"):
                        t += f"  ({gpu['temp_note']})"
                    g.set("temp", t)
                elif rt == "suspended":
                    g.set("temp", "— (powered off)")
                else:
                    g.set("temp", "—")

                g.set("volt", f"{gpu['voltage_v']:.2f} V" if gpu["voltage_v"] is not None else "—")

                if gpu["clock_max"]:
                    cur = f"{gpu['clock_cur']}" if gpu["clock_cur"] else "—"
                    g.set("clock", f"{cur} / {gpu['clock_max']} MHz")
                else:
                    g.set("clock", "—")

                if gpu["busy_percent"] is not None:
                    g.set("busy", _pct(gpu["busy_percent"]))
                else:
                    g.set("busy", "not exposed by driver")

                # amdgpu reports live VRAM usage; refine the static row when present
                if gpu.get("vram_total"):
                    g.set("vram", f"{hw.fmt_bytes(gpu['vram_used'])} / {hw.fmt_bytes(gpu['vram_total'])}")

        self._updaters.append(update)
        return _page(*groups) if groups else _page(StatGroup("Graphics"))

    def _build_storage(self):
        groups = []
        disks = StatGroup("Drives")
        for d in self.static["storage"]["disks"]:
            disks.add(
                d["name"],
                f"{d['model']} ({d['name']})",
                f"{hw.fmt_bytes(d['size'])} · {d['type']} · {d['bus']} · health {d['health']}",
            )
        # live temp row for the NVMe drive
        disks.add("nvmetemp", "NVMe Temperature")
        groups.append(disks)

        fs = StatGroup("Filesystems")
        self._fs_keys = []
        for i, p in enumerate(self.static["storage"]["partitions"]):
            fs.add_bar(f"fs{i}", f"{p['mount']}  ({p['fstype']})")
            self._fs_keys.append((f"fs{i}", p["mount"]))
        groups.append(fs)

        def update(dyn):
            disks.set("nvmetemp", _temp(dyn["disk_temp"]))
            parts = {p["mount"]: p for p in dyn["partitions"]}
            for key, mount in self._fs_keys:
                p = parts.get(mount)
                if p:
                    fs.set_bar(key, p["percent"],
                               f"{hw.fmt_bytes(p['used'])} / {hw.fmt_bytes(p['total'])}")

        self._updaters.append(update)
        return _page(*groups)

    def _build_sensors(self):
        # Built dynamically from the first dynamic read so labels are stable.
        self._sensors_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._sensors_built = False
        clamp = Adw.Clamp(maximum_size=820, child=self._sensors_box)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(18)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(clamp)

        def update(dyn):
            if not self._sensors_built:
                for chip in dyn["chips"]:
                    g = StatGroup(chip["name"])
                    for t in chip["temps"]:
                        g.add(("t", chip["name"], t["label"]), t["label"])
                    for fobj in chip["fans"]:
                        g.add(("f", chip["name"], fobj["label"]), fobj["label"])
                    for v in chip["volts"]:
                        g.add(("v", chip["name"], v["label"]), v["label"])
                    self._sensors_box.append(g)
                    self._sensor_labels[chip["name"]] = g
                self._sensors_built = True
            for chip in dyn["chips"]:
                g = self._sensor_labels.get(chip["name"])
                if not g:
                    continue
                for t in chip["temps"]:
                    g.set(("t", chip["name"], t["label"]), _temp(t["value_c"]))
                for fobj in chip["fans"]:
                    g.set(("f", chip["name"], fobj["label"]), f"{fobj['rpm']} RPM")
                for v in chip["volts"]:
                    g.set(("v", chip["name"], v["label"]), f"{v['v']:.2f} V")

        self._updaters.append(update)
        return scroller

    def _build_network(self):
        self._net_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._net_built = False
        self._net_groups = {}
        clamp = Adw.Clamp(maximum_size=820, child=self._net_box)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(18)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(clamp)

        def update(dyn):
            if not self._net_built:
                for n in dyn["network"]:
                    g = StatGroup(n["name"])
                    g.add("status", "Status")
                    g.add("ipv4", "IPv4")
                    g.add("ipv6", "IPv6")
                    g.add("mac", "MAC")
                    g.add("speed", "Link Speed")
                    g.add("traffic", "Recv / Sent")
                    self._net_box.append(g)
                    self._net_groups[n["name"]] = g
                self._net_built = True
            for n in dyn["network"]:
                g = self._net_groups.get(n["name"])
                if not g:
                    continue
                g.set("status", "Up" if n["up"] else "Down")
                g.set("ipv4", n["ipv4"])
                g.set("ipv6", n["ipv6"])
                g.set("mac", n["mac"])
                g.set("speed", f"{n['speed_mbps']} Mb/s" if n["speed_mbps"] else "—")
                g.set("traffic", f"{hw.fmt_bytes(n['recv'])} / {hw.fmt_bytes(n['sent'])}")

        self._updaters.append(update)
        return scroller

    def _build_system(self):
        s = self.static["system"]
        osg = StatGroup("Operating System")
        osg.add("os", "Distribution", s["os_name"])
        osg.add("kernel", "Kernel", s["kernel"])
        osg.add("arch", "Architecture", s["arch"])
        osg.add("host", "Hostname", s["hostname"])
        osg.add("uptime", "Uptime")

        mb = StatGroup("Motherboard")
        mb.add("vendor", "Manufacturer", s["board_vendor"])
        mb.add("board", "Model", s["board_name"])
        mb.add("product", "System", f"{s['sys_vendor']} {s['product']}")

        bios = StatGroup("BIOS / Firmware")
        bios.add("bvendor", "Vendor", s["bios_vendor"])
        bios.add("bver", "Version", s["bios_version"])
        bios.add("bdate", "Date", s["bios_date"])

        def update(dyn):
            osg.set("uptime", hw.uptime_str(dyn["system_live"]["uptime_s"]))

        self._updaters.append(update)
        return _page(osg, mb, bios)

    # ---- helpers --------------------------------------------------------
    def static_cpu(self):
        if not hasattr(self, "_cpu_static"):
            self._cpu_static = hw.read_cpu()
        return self._cpu_static

    def static_cpu_model(self):
        return self.static_cpu()["model"]

    @staticmethod
    def _short(name):
        # trim "Vendor Corporation" verbosity for the summary line
        return name.replace(" Corporation", "").replace(" Inc.", "")

    # ---- live tick ------------------------------------------------------
    def _tick(self):
        dyn = hw.read_dynamic(self._pci, self._opengl)
        for fn in self._updaters:
            try:
                fn(dyn)
            except Exception as e:  # never let a UI glitch kill the timer
                print("update error:", e)
        cpu_t = dyn["cpu"]["package_temp"]
        self._subtitle.set_label(
            f"CPU {_temp(cpu_t)} · {_pct(dyn['cpu']['usage_total'])}   |   live"
        )
        return True  # keep firing
