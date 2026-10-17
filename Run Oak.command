#!/usr/bin/env bash
# Double-click to start PCB Studio as a desktop app (runs in Terminal, which already has access to this folder).
cd "$(dirname "$0")"
exec .venv/bin/python app.py
