#!/usr/bin/env python3
"""
run_laptop_bluetooth.py — open this in Thonny and press Run. Nothing
else to configure.

Runs mindwave_pico_bridge.py with EEG data coming from OpenViBE
(the default source) and sent to the Pi 5 over Bluetooth — same as
running: py mindwave_pico_bridge.py --link bluetooth
Must sit in the same folder as mindwave_pico_bridge.py.
"""
import runpy
import sys

sys.argv = [sys.argv[0], "--link", "bluetooth"]
runpy.run_path("mindwave_pico_bridge.py", run_name="__main__")
