#!/usr/bin/env python3
"""Passive serial logger: reset the board cleanly, then only READ (send
nothing), so the board enters provisioning mode rather than USB mode.
Usage: python3 tools/passive_log.py [/dev/cu.usbserial-14420] | tee board.log
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbserial-14420"
ser = serial.Serial()
ser.port = port
ser.baudrate = 115200
ser.timeout = 0.2
ser.dtr = False
ser.rts = False
ser.open()
ser.dtr = False
ser.rts = True          # pulse EN: clean reset into run mode
time.sleep(0.15)
ser.rts = False
time.sleep(0.3)
ser.reset_input_buffer()
sys.stderr.write(f"[passive] reading {port}, sending nothing\n")
while True:
    data = ser.read(512)
    if data:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
