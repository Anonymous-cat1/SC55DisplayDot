#!/usr/bin/env python3
"""
DisplayDotScreen: Capture a screen, convert to 16x16 monochrome, and send as Roland SC-55MkII SysEx.

Usage:
    displaydotscreen.py --monitor 1 [--port PORT] [--device-id ID]
                       [--threshold N] [--contrast C] [--auto-adjust]
                       [--invert] [--loop] [--fps FPS]

Keybinds (loop mode):
    + / -       : Increase / decrease threshold
    ] / [       : Increase / decrease contrast
    q           : Quit loop

Requires:
    - Python 3
    - mido (for MIDI I/O)
    - mss (for screen capture)
    - Pillow (for image processing)
    - numpy
    - keyboard (for keybinds)
"""
import sys
import argparse
import time
import threading

try:
    import mido
    MIDO_AVAILABLE = True
except ImportError:
    MIDO_AVAILABLE = False

try:
    import mss
    from PIL import Image, ImageEnhance, ImageOps
    import numpy as np
    import keyboard
except ImportError as e:
    print(f"Missing dependency: {e.name}. Install with pip.")
    sys.exit(1)

# Roland GS SysEx constants
MANUF_ID = 0x41
MODEL_ID = 0x45
SUB_ID1 = 0x12
SUB_ID2 = 0x10
BLOCK    = 0x01
MSB      = 0x00
LSB      = 0x10
DEVICE_ID_DEFAULT = 0x10


def grid_to_sysex(grid, device_id=DEVICE_ID_DEFAULT):
    data = [0] * 64
    for r in range(16):
        for c in range(16):
            if grid[r][c]:
                if c < 5:
                    idx, bit = r, 4 - c
                elif c < 10:
                    idx, bit = 16 + r, 9 - c
                elif c < 15:
                    idx, bit = 32 + r, 14 - c
                else:
                    idx, bit = 48 + r, 19 - c
                data[idx] |= (1 << bit)
    address = [BLOCK, MSB]
    syx = [0xF0, MANUF_ID, device_id, MODEL_ID, SUB_ID1, SUB_ID2] + address + data
    checksum = ((128 - (sum(address + [LSB] + data) % 128))) & 0x7F
    syx += [checksum, 0xF7]
    return syx


def capture_and_process(monitor_number, threshold, contrast, invert, auto_adjust):
    with mss.mss() as sct:
        monitors = sct.monitors
        try:
            mon = monitors[monitor_number]
        except IndexError:
            print(f"Invalid monitor index {monitor_number}. Available: 1-{len(monitors)-1}")
            sys.exit(1)
        sct_img = sct.grab(mon)
        img = Image.frombytes('RGB', sct_img.size, sct_img.rgb)

    # resize
    img_small = img.resize((16, 16), Image.BILINEAR)
    gray = img_small.convert('L')

    # auto adjust if requested
    if auto_adjust:
        gray = ImageOps.autocontrast(gray)
        thresh_val = np.array(gray).mean()
    else:
        thresh_val = threshold

    # apply contrast
    enhancer = ImageEnhance.Contrast(gray)
    gray = enhancer.enhance(contrast)

    arr = np.array(gray)
    grid = (arr >= thresh_val).astype(np.uint8)
    if invert:
        grid = 1 - grid
    return grid.tolist(), thresh_val


def send_sysex(sysex, port_name):
    if not MIDO_AVAILABLE:
        print("mido library not available. Cannot send SysEx.")
        return
    try:
        with mido.open_output(port_name) as out:
            out.send(mido.Message('sysex', data=sysex[1:-1]))
    except IOError as e:
        print(f"Failed to open MIDI port {port_name}: {e}")


def list_ports():
    if not MIDO_AVAILABLE:
        return []
    return mido.get_output_names()


def open_port(port_arg):
    port = port_arg or (list_ports()[0] if list_ports() else None)
    if port:
        print(f"Using MIDI port: {port}")
    else:
        print("No MIDI ports available. Will skip sending.")
    return port


def main():
    p = argparse.ArgumentParser()
    p.add_argument('-m', '--monitor', type=int, default=1)
    p.add_argument('-t', '--threshold', type=int, default=128)
    p.add_argument('-c', '--contrast', type=float, default=1.0)
    p.add_argument('-a', '--auto-adjust', action='store_true')
    p.add_argument('-i', '--invert', action='store_true')
    p.add_argument('-l', '--loop', action='store_true')
    p.add_argument('-f', '--fps', type=float, default=None)
    p.add_argument('-p', '--port', default=None)
    p.add_argument('-d', '--device-id', type=lambda x: int(x, 0), default=DEVICE_ID_DEFAULT)
    p.add_argument('--list', action='store_true')
    args = p.parse_args()

    if args.list:
        for n in list_ports(): print(f"  {n}")
        return

    # defaults
    if args.loop is False:
        args.loop = True
    if args.invert is False:
        args.invert = True
    if args.fps is None:
        args.fps = 120.0

    port = open_port(args.port)
    delay = 1.0 / args.fps
    print(f"Loop mode: {args.loop}, invert: {args.invert}, fps: {args.fps:.1f}, delay: {delay:.3f}s")

    running = True
    # keybind thread
    def kb_thread():
        nonlocal running, args
        while running:
            if keyboard.is_pressed('+'):
                args.threshold = min(255, args.threshold + 5)
            if keyboard.is_pressed('-'):
                args.threshold = max(0, args.threshold - 5)
            if keyboard.is_pressed(']'):
                args.contrast = min(5.0, args.contrast + 0.25)
            if keyboard.is_pressed('['):
                args.contrast = max(0.0, args.contrast - 0.25)
            if keyboard.is_pressed('q'):
                running = False
            time.sleep(0.05)

    print("Keys: + / - = threshold, ] / [ = contrast, q = quit")
    th_val = args.threshold
    ct_val = args.contrast

    if args.loop:
        t = threading.Thread(target=kb_thread, daemon=True)
        t.start()
        try:
            while running:
                grid, used_thresh = capture_and_process(
                    args.monitor, args.threshold,
                    args.contrast, args.invert,
                    args.auto_adjust)
                syx = grid_to_sysex(grid, device_id=args.device_id)
                send_sysex(syx, port)
                if (args.threshold != th_val) or (args.contrast != ct_val):
                    print(f"Threshold={args.threshold}, Contrast={args.contrast:.1f}")
                    th_val, ct_val = args.threshold, args.contrast
                time.sleep(delay)
        except KeyboardInterrupt:
            pass
    else:
        grid, _ = capture_and_process(
            args.monitor, args.threshold,
            args.contrast, args.invert,
            args.auto_adjust)
        syx = grid_to_sysex(grid, device_id=args.device_id)
        send_sysex(syx, port)

if __name__ == '__main__':
    main()
