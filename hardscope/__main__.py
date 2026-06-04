"""Application entry point: `python3 -m hardscope`."""

import sys

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from .window import HardScopeWindow  # noqa: E402


class HardScopeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.hardscope.HardScope")

    def do_activate(self):
        win = self.props.active_window or HardScopeWindow(self)
        win.present()


def main():
    return HardScopeApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
