#!/bin/bash
# Launcher used by the .desktop file to run HardScope as root.
# pkexec strips most env vars, so we forward the display explicitly.
exec pkexec env \
  DISPLAY="$DISPLAY" \
  WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
  XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
  PYTHONPATH="/home/edge/hardscope" \
  python3 -m hardscope
