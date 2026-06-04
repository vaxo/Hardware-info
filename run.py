#!/usr/bin/env python3
"""Convenience launcher: `./run.py` (same as `python3 -m hardscope`)."""
from hardscope.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
