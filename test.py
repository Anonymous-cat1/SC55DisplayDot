#!/usr/bin/env python3
"""
roland_screen_pwm_gui_robust.py

Screen -> Roland GS SysEx 4-level display with live GUI preview (Tkinter).
Preview is NOT inverted (what you see is what's captured); device output IS inverted
by default (lit LEDs appear black on the SC-55).

Improved robustness: MIDI sending is done in a dedicated MidiSender thread that
reads the latest frame and continuously outputs PWM planes. It attempts to recover
from send errors by reopening the MIDI port.

Usage:
    python roland_screen_pwm_gui_robust.py [--port N] [--device 0x10] [--fps 30] [--base_ms 39]
                                           [--no-pwm] [--dither] [--gamma 1.0] [--monitor 1]
                                           [--nogui] [--no-invert]

Controls while running (stdin):
    ]    : increase speed (decrease base_ms)
    [    : decrease speed (increase base_ms)
    p    : pause / resume
    q    : quit
"""

import argparse
import threading
import time
import queue
import os
import sys
import traceback

# External libs
try:
    import mido
    import mss
    from PIL import Image, ImageTk
    import numpy as np
    import tkinter as tk
except Exception as e:
    print("Missing dependency:", e)
    print("Install: pip install mido python-rtmidi mss pillow numpy")
    raise

# Roland GS SysEx constants
MANUF_ID = 0x41
MODEL_ID = 0x45
SUB_ID1 = 0x12
SUB_ID2 = 0x10
BLOCK    = 0x01
MSB      = 0x00
LSB      = 0x10

FIXED_BITDEPTH = 2  # 2 bits -> 4 grey levels (0..3)

# --- SysEx helpers ---
def compute_checksum(address, data):
    s = sum(address + [LSB] + data)
    checksum = ((128 - (s % 128))) & 0x7F
    return checksum

def brightness_boolgrid_to_sysex(bool_grid, device_id=0x10):
    """Convert 16x16 bool grid to Roland SysEx payload bytes."""
    data = [0] * 64
    for r in range(16):
        for c in range(16):
            if bool_grid[r][c]:
                if   c <  5: idx, bit =   r, 4 - c
                elif c < 10: idx, bit = 16+r, 9 - c
                elif c < 15: idx, bit = 32+r,14 - c
                else:        idx, bit = 48+r,19 - c
                data[idx] |= (1 << bit)
    address = [BLOCK, MSB]
    payload = [MANUF_ID, device_id, MODEL_ID, SUB_ID1, SUB_ID2] + address + data
    checksum = compute_checksum(address, data)
    return payload + [checksum]

def make_bitplane_boolgrid(level_grid, plane):
    """Given python-list 16x16 of 0..3, return 16x16 bool grid for bitplane."""
    mask = 1 << plane
    return [[1 if (level_grid[r][c] & mask) else 0 for c in range(16)] for r in range(16)]

# --- Image processing ---
def percentile_contrast_stretch(arr, p_lo=1.0, p_hi=99.0):
    low = np.percentile(arr, p_lo)
    high = np.percentile(arr, p_hi)
    if high <= low:
        return np.clip(arr, 0, 255).astype(np.uint8)
    out = (arr - low) * (255.0 / (high - low))
    out = np.clip(out, 0, 255)
    return out.astype(np.uint8)

BAYER_4x4 = (1.0 / 17.0) * np.array([
    [0,  8,  2, 10],
    [12, 4, 14,  6],
    [3, 11,  1,  9],
    [15, 7, 13,  5]
], dtype=np.float32)

def ordered_bayer_dither(arr_0_255, levels=4):
    H, W = arr_0_255.shape
    out = np.zeros_like(arr_0_255, dtype=np.uint8)
    for y in range(H):
        for x in range(W):
            threshold = BAYER_4x4[y % 4, x % 4] * 255.0
            val = arr_0_255[y, x]
            scaled = val + threshold
            level = int(np.floor(scaled * (levels) / 256.0))
            level = max(0, min(levels - 1, level))
            out[y, x] = level
    return out

# --- Capture & downscale ---
def capture_and_downscale(monitor=1, width=16, height=16, method=Image.BILINEAR):
    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 1 or monitor > len(monitors)-1:
            bbox = monitors[0]
        else:
            bbox = monitors[monitor]
        img = sct.grab(bbox)
        pil = Image.frombytes("RGB", img.size, img.rgb)
        pil = pil.convert("L")
        pil = pil.resize((width, height), method)
        arr = np.array(pil, dtype=np.uint8)
        return arr

# --- keyboard reader (stdin) ---
def key_reader_loop(key_q):
    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            key_q.put(ch)
            if ch == 'q':
                break
    else:
        import sys, termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                key_q.put(ch)
                if ch == 'q':
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

# --- responsive helpers ---
def process_keys_now(key_q, state):
    changed_speed = False
    while not key_q.empty():
        try:
            k = key_q.get_nowait()
        except queue.Empty:
            break
        if k == ']':
            state['base_ms'] = max(1.0, state['base_ms'] - 0.1)
            print(f"[key] faster -> base_ms={state['base_ms']:.1f} ms")
            changed_speed = True
        elif k == '[':
            state['base_ms'] += 0.1
            print(f"[key] slower -> base_ms={state['base_ms']:.1f} ms")
            changed_speed = True
        elif k in ('p','P'):
            state['paused'] = not state['paused']
            print("[key] paused" if state['paused'] else "[key] resumed")
        elif k == 'q':
            print("[key] quit")
            state['running'] = False
            return True, changed_speed
    return False, changed_speed

def wait_with_checks(duration_s, key_q, state, tick=0.01):
    start = time.time()
    while (time.time() - start) < duration_s:
        quit_req, speed_changed = process_keys_now(key_q, state)
        if quit_req:
            return True, False
        if speed_changed:
            return False, True
        if state['paused']:
            time.sleep(tick)
            continue
        time_left = duration_s - (time.time() - start)
        time.sleep(min(tick, max(0.0, time_left)))
    return False, False

# --- MidiSender thread: reads latest_frame and drives PWM ---
class MidiSender(threading.Thread):
    def __init__(self, port_name, args, state, key_q, latest_frame_holder):
        super().__init__(daemon=True)
        self.port_name = port_name
        self.args = args
        self.state = state
        self.key_q = key_q
        self.latest_frame_holder = latest_frame_holder  # {'lock':..., 'level_grid':numpy array or None}
        self.out = None
        self._last_send_time = 0.0

    def open_port(self):
        # try to open using the saved port name; swallow exceptions
        try:
            if self.out is not None:
                try:
                    self.out.close()
                except Exception:
                    pass
            print(f"[midi] opening port '{self.port_name}'")
            self.out = mido.open_output(self.port_name)
            print("[midi] opened")
            return True
        except Exception as e:
            print("[midi] open failed:", e)
            self.out = None
            return False

    def safe_send(self, msg):
        # wrapper around out.send with exception handling
        if self.out is None:
            raise RuntimeError("MIDI port not open")
        try:
            self.out.send(msg)
            self._last_send_time = time.time()
            return True
        except Exception as e:
            print("[midi] send error:", e)
            # attempt to close underlying port reference
            try:
                self.out.close()
            except Exception:
                pass
            self.out = None
            return False

    def get_latest_frame(self):
        with self.latest_frame_holder['lock']:
            frame = self.latest_frame_holder.get('level_grid', None)
            ts = self.latest_frame_holder.get('timestamp', 0.0)
        return frame, ts

    def run(self):
        # open port initially
        backoff = 0.5
        max_backoff = 8.0
        if not self.open_port():
            # try a few times before entering main loop
            while self.state['running'] and backoff <= max_backoff:
                time.sleep(backoff)
                if self.open_port():
                    break
                backoff = min(max_backoff, backoff * 2.0)

        print("[midi] sender started")
        try:
            while self.state['running']:
                # handle keys aggressively
                quit_req, _ = process_keys_now(self.key_q, self.state)
                if quit_req:
                    break

                if self.state['paused']:
                    time.sleep(0.05)
                    continue

                frame, ts = self.get_latest_frame()
                if frame is None:
                    # nothing to send yet
                    time.sleep(0.005)
                    continue

                # produce a local python list-of-lists copy (immutable snapshot)
                try:
                    # if frame is numpy array convert; frame might already be list
                    if isinstance(frame, np.ndarray):
                        level_grid = frame.copy().tolist()
                    else:
                        # list-of-lists
                        level_grid = [row[:] for row in frame]
                except Exception:
                    level_grid = frame

                # if invert requested, apply inversion for device output; keep preview unchanged
                if self.args.invert:
                    # produce send_grid = 3 - level
                    send_grid = [[3 - int(level_grid[r][c]) for c in range(16)] for r in range(16)]
                else:
                    send_grid = [[int(level_grid[r][c]) for c in range(16)] for r in range(16)]

                # PWM loop for this snapshot: plane 0 then plane 1 (LSB then MSB)
                for plane in range(FIXED_BITDEPTH):
                    if not self.state['running']:
                        break
                    quit_req, speed_changed = process_keys_now(self.key_q, self.state)
                    if quit_req:
                        self.state['running'] = False
                        break
                    # build and send payload
                    bitgrid = make_bitplane_boolgrid(send_grid, plane)
                    payload = brightness_boolgrid_to_sysex(bitgrid, device_id=self.args.device)
                    msg = mido.Message('sysex', data=payload)

                    # ensure port open
                    if self.out is None:
                        opened = self.open_port()
                        if not opened:
                            # wait a bit and continue to next loop (will retry)
                            time.sleep(0.2)
                            continue

                    ok = self.safe_send(msg)
                    if not ok:
                        # attempt to reopen with exponential backoff
                        backoff = 0.5
                        while self.state['running'] and backoff <= 8.0:
                            print(f"[midi] retry open in {backoff:.1f}s")
                            time.sleep(backoff)
                            if self.open_port():
                                break
                            backoff = min(8.0, backoff * 2.0)
                        # after attempting reopen, continue to next plane (or will retry send)
                        continue

                    # wait for the plane duration but remain responsive.
                    dur_s = (self.state['base_ms'] * (2 ** plane)) / 1000.0
                    # During wait, break early if speed changed
                    qquit, sp_changed = wait_with_checks(dur_s, self.key_q, self.state, tick=0.01)
                    if qquit:
                        self.state['running'] = False
                        break
                    if sp_changed:
                        # apply new speed sooner: break plane loop
                        break

                # quick watchdog: if no successful send for > 2s, try reopen
                if self._last_send_time and (time.time() - self._last_send_time) > 2.0:
                    print("[midi] watchdog: no sends > 2s, checking port")
                    if self.out is None:
                        self.open_port()

                # small yield
                time.sleep(0.001)

        except Exception as e:
            print("[midi] unexpected sender error:", e)
            traceback.print_exc()
        finally:
            try:
                if self.out is not None:
                    self.out.close()
            except Exception:
                pass
            self.state['running'] = False
            print("[midi] sender exiting")

# --- Capture thread: captures, processes, writes latest_frame and preview ---
class CaptureThread(threading.Thread):
    def __init__(self, args, state, key_q, latest_frame_holder, preview_holder):
        super().__init__(daemon=True)
        self.args = args
        self.state = state
        self.key_q = key_q
        self.latest_frame_holder = latest_frame_holder
        self.preview_holder = preview_holder

    def run(self):
        target_frame_time = 1.0 / min(max(1.0, self.args.fps), 60.0)
        print(f"[capture] target FPS {self.args.fps} -> frame time {target_frame_time*1000:.1f} ms")
        try:
            while self.state['running']:
                loop_start = time.time()
                # check keys early
                quit_req, _ = process_keys_now(self.key_q, self.state)
                if quit_req:
                    break
                if self.state['paused']:
                    time.sleep(0.05)
                    continue

                img = capture_and_downscale(monitor=self.args.monitor, width=16, height=16)
                img = percentile_contrast_stretch(img, p_lo=1.0, p_hi=99.0)

                if self.args.gamma != 1.0 and self.args.gamma > 0.0:
                    lut = np.array([((i/255.0) ** (1.0/self.args.gamma)) * 255.0 for i in range(256)], dtype=np.uint8)
                    img = lut[img]

                # map to 4 levels
                if self.args.dither and self.args.no_pwm:
                    lvl_grid = ordered_bayer_dither(img, levels=4)
                else:
                    lvl_grid = ((img.astype(np.float32) * (4.0 / 256.0))).astype(np.int32)
                    lvl_grid = np.clip(lvl_grid, 0, 3).astype(np.uint8)

                # store latest frame (non-inverted) for sender to read; sender will invert if needed
                with self.latest_frame_holder['lock']:
                    self.latest_frame_holder['level_grid'] = lvl_grid.copy()
                    self.latest_frame_holder['timestamp'] = time.time()

                # prepare preview (non-inverted) for GUI
                if not self.args.nogui:
                    preview_arr = (lvl_grid.astype(np.float32) * (255.0/3.0)).astype(np.uint8)
                    pil_preview = Image.fromarray(preview_arr, mode='L').resize((160,160), Image.NEAREST).convert("RGB")
                    with self.preview_holder['lock']:
                        self.preview_holder['image'] = pil_preview

                # throttle capture loop to target FPS
                elapsed = time.time() - loop_start
                to_wait = max(0.0, target_frame_time - elapsed)
                # remain responsive while waiting
                qquit, _ = wait_with_checks(to_wait, self.key_q, self.state, tick=0.01)
                if qquit:
                    break

        except Exception as e:
            print("[capture] unexpected error:", e)
            traceback.print_exc()
        finally:
            self.state['running'] = False
            print("[capture] thread exiting")

# --- GUI (Tk) main app ---
class PreviewApp:
    def __init__(self, root, preview_holder):
        self.root = root
        self.preview_holder = preview_holder
        root.title("Preview (non-inverted) - device output remains inverted")
        self.canvas = tk.Canvas(root, width=160, height=160)
        self.canvas.pack()
        self.photo = None
        self.img_id = None
        self.update_interval_ms = 33  # ~30 FPS preview
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.running = True
        self._update()

    def _update(self):
        if not self.running:
            return
        with self.preview_holder['lock']:
            pil_image = self.preview_holder.get('image', None)
        if pil_image is not None:
            self.photo = ImageTk.PhotoImage(pil_image)
            if self.img_id is None:
                self.img_id = self.canvas.create_image(0,0, anchor="nw", image=self.photo)
            else:
                self.canvas.itemconfig(self.img_id, image=self.photo)
        self.root.after(self.update_interval_ms, self._update)

    def on_close(self):
        self.running = False
        try:
            self.root.quit()
        except Exception:
            pass

# --- MAIN ---
def main():
    parser = argparse.ArgumentParser(description="Screen -> Roland GS SysEx 4-level display (robust sender)")
    parser.add_argument("--port", "-p", type=int, default=0, help="MIDI output port index (default 0).")
    parser.add_argument("--device", "-d", type=lambda s: int(s,0), default=0x10, help="Device ID (hex ok).")
    parser.add_argument("--fps", type=float, default=30.0, help="Capture FPS target (max). Default 30. Set up to 60.")
    parser.add_argument("--base_ms", type=float, default=51.2, help="Base ms for LSB plane. Default 39ms.")
    parser.add_argument("--no-pwm", action="store_true", help="Disable PWM (send only MSB plane).")
    parser.add_argument("--dither", action="store_true", help="Apply ordered Bayer dithering (useful when -no-pwm).")
    parser.add_argument("--gamma", type=float, default=1.0, help="Gamma correction (applied before mapping). Default 1.0 (none).")
    parser.add_argument("--monitor", type=int, default=1, help="Which monitor to capture (mss monitor index).")
    parser.add_argument("--nogui", action="store_true", help="Disable GUI preview.")
    parser.add_argument("--no-invert", action="store_true", help="Disable default inversion (by default invert so lit = black).")
    args = parser.parse_args()

    # choose port by index
    try:
        ports = mido.get_output_names()
        if not ports:
            print("No MIDI outputs detected. Exiting.")
            return
        if args.port < 0 or args.port >= len(ports):
            print("Available MIDI outputs:")
            for i, p in enumerate(ports):
                print(f"[{i}] {p}")
            print("Use --port N to select.")
            return
        port_name = ports[args.port]
    except Exception as e:
        print("MIDI port selection error:", e)
        return

    print(f"[main] using MIDI output: {port_name}")
    # note: MidiSender will open the port itself to allow reopen attempts.

    key_q = queue.Queue()
    kr = threading.Thread(target=key_reader_loop, args=(key_q,), daemon=True)
    kr.start()

    state = {
        'paused': False,
        'base_ms': float(args.base_ms),
        'running': True
    }

    # shared holders
    latest_frame_holder = {'lock': threading.Lock(), 'level_grid': None, 'timestamp': 0.0}
    preview_holder = {'lock': threading.Lock(), 'image': None}

    # map args flags
    args.no_pwm = args.no_pwm
    args.dither = args.dither
    args.gamma = args.gamma
    args.monitor = args.monitor
    args.device = args.device
    args.fps = args.fps
    args.nogui = args.nogui
    args.invert = not args.no_invert  # default True unless --no-invert passed

    # start threads
    midi_sender = MidiSender(port_name, args, state, key_q, latest_frame_holder)
    capture_thread = CaptureThread(args, state, key_q, latest_frame_holder, preview_holder)

    midi_sender.start()
    capture_thread.start()

    # GUI in main thread if desired
    if not args.nogui:
        root = tk.Tk()
        app = PreviewApp(root, preview_holder)
        try:
            def poll():
                if not state['running'] or not midi_sender.is_alive() or not capture_thread.is_alive() or not app.running:
                    state['running'] = False
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                root.after(200, poll)
            root.after(200, poll)
            root.mainloop()
        except KeyboardInterrupt:
            print("[main] GUI interrupted by user.")
        finally:
            state['running'] = False
    else:
        try:
            while state['running'] and midi_sender.is_alive() and capture_thread.is_alive():
                time.sleep(0.1)
        except KeyboardInterrupt:
            state['running'] = False

    # join threads and close
    state['running'] = False
    midi_sender.join(timeout=1.0)
    capture_thread.join(timeout=1.0)
    print("[main] exiting")

if __name__ == "__main__":
    main()
