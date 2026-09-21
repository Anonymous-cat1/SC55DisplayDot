#!/usr/bin/env python3
"""
roland_screen_sysex.py

A tool to capture a region of your screen, downscale it to 16x16 pixels,
convert it to 4-level grayscale, and send it to a Roland GS-compatible
device's LCD display via MIDI SysEx messages.

Features:
- Live preview of the 16x16 output.
- 4-level grayscale display using software Pulse-Width Modulation (PWM).
- Automatic contrast stretching for clear images.
- Ability to record the SysEx output to a standard MIDI file.
- Configurable PWM timing, MIDI device ID, monitor, and more.
"""

import argparse
import threading
import time
import queue
import sys
import traceback
from typing import List, Optional

# --- Dependency Check ---
try:
    import mido
    import mido.backends.rtmidi
    import mss
    from PIL import Image, ImageTk
    import numpy as np
    import tkinter as tk
except ImportError as e:
    print(f"Missing dependency: {e.name}", file=sys.stderr)
    print("Please install the required libraries:", file=sys.stderr)
    print("pip install mido python-rtmidi mss pillow numpy", file=sys.stderr)
    sys.exit(1)

# --- Roland GS SysEx Constants ---
MANUF_ID = 0x41  # Roland
MODEL_ID = 0x45  # GS
CMD_DT1 = 0x12   # Data Set 1
CMD_SUB_ID = 0x10 # Individual Parameter (SC-55 specific)
# Address for 16x16 LCD display
ADDR_BLOCK = 0x01
ADDR_MSB = 0x00
ADDR_LSB = 0x10

# --- SysEx Generation Logic ---

def grid_to_sysex_payload(grid: np.ndarray, device_id: int) -> List[int]:
    """
    Converts a 16x16 boolean numpy array into a full Roland GS SysEx message
    payload for the LCD display.
    """
    if grid.shape != (16, 16):
        raise ValueError("Input grid must be 16x16")

    # The 16x16 grid is mapped to 64 data bytes. Each byte controls a
    # 16-pixel high column segment, but the bits are arranged strangely.
    data = [0] * 64
    for r in range(16):
        for c in range(16):
            if grid[r, c]:
                # This mapping is specific to the Roland SC-55/88 display memory layout.
                if c < 5:
                    idx, bit = r, 4 - c
                elif c < 10:
                    idx, bit = 16 + r, 9 - c
                elif c < 15:
                    idx, bit = 32 + r, 14 - c
                else:  # c == 15
                    idx, bit = 48 + r, 19 - c
                data[idx] |= (1 << bit)

    # The address in the message body is only 2 bytes.
    address = [ADDR_BLOCK, ADDR_MSB]
    # The checksum calculation, however, includes the LSB. This is a known
    # quirk for this specific SysEx message on the SC-55.
    total_for_checksum = sum(address + [ADDR_LSB] + data)
    checksum = (128 - (total_for_checksum % 128)) & 0x7F

    # Full SysEx message structure
    # NOTE: The CMD_SUB_ID (0x10) is crucial for the SC-55 to correctly
    # parse the message and avoid an "Address Error".
    payload = [
        MANUF_ID,
        device_id,
        MODEL_ID,
        CMD_DT1,
        CMD_SUB_ID,
        *address,
        *data,
        checksum
    ]
    return payload

# --- Image Processing Logic ---

def capture_and_process(monitor_idx: int, contrast_low: float, contrast_high: float) -> np.ndarray:
    """Captures a monitor, resizes to 16x16, and applies contrast."""
    with mss.mss() as sct:
        monitors = sct.monitors
        # mss.monitors includes a full-desktop view at index 0.
        # We use index 1 as the default "main" monitor.
        if monitor_idx >= len(monitors) or monitor_idx < 0:
            print(f"Warning: Monitor {monitor_idx} not found. Defaulting to monitor 1.", file=sys.stderr)
            monitor_idx = 1

        bbox = monitors[monitor_idx]
        sct_img = sct.grab(bbox)

        pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        pil_img_gray = pil_img.convert("L")
        pil_img_resized = pil_img_gray.resize((16, 16), Image.Resampling.BILINEAR)

        # Convert to numpy array for processing
        arr = np.array(pil_img_resized, dtype=np.float32)

        # Automatic contrast stretch based on percentiles
        low = np.percentile(arr, contrast_low)
        high = np.percentile(arr, contrast_high)
        if high <= low: high = low + 1 # Avoid division by zero

        arr = (arr - low) * (255.0 / (high - low))
        return np.clip(arr, 0, 255).astype(np.uint8)

# --- Core Application Threads ---

class CaptureThread(threading.Thread):
    """Continuously captures and processes screen frames."""
    def __init__(self, args, frame_queue: queue.Queue, running_event: threading.Event):
        super().__init__(daemon=True)
        self.args = args
        self.frame_queue = frame_queue
        self.running_event = running_event
        self.name = "CaptureThread"

    def run(self):
        target_frame_time = 1.0 / self.args.fps
        while self.running_event.is_set():
            loop_start = time.perf_counter()

            try:
                # 1. Capture and process the image
                img_8bit = capture_and_process(self.args.monitor, self.args.contrast_low, self.args.contrast_high)

                # 2. Convert to 4-level grayscale (0-3)
                level_grid = (img_8bit.astype(np.float32) * (3.999 / 255.0)).astype(np.uint8)

                # 3. Put the result in the queue for the other threads
                try:
                    # Non-blocking put, discard old frame if queue is full
                    self.frame_queue.put_nowait(level_grid)
                except queue.Full:
                    pass # It's okay to drop frames

            except Exception:
                print("Error in capture thread:", file=sys.stderr)
                traceback.print_exc()
                time.sleep(1) # Avoid spamming errors

            # Throttle to the target FPS
            elapsed = time.perf_counter() - loop_start
            wait_time = max(0, target_frame_time - elapsed)
            time.sleep(wait_time)


class MidiSenderThread(threading.Thread):
    """Sends frames from a queue to a MIDI port or records to a file."""
    def __init__(self, args, frame_queue: queue.Queue, running_event: threading.Event):
        super().__init__(daemon=True)
        self.args = args
        self.frame_queue = frame_queue
        self.running_event = running_event
        self.name = "MidiSenderThread"

    def run(self):
        if self.args.record:
            self._record_loop()
        else:
            self._live_loop()

    def _get_latest_frame(self) -> Optional[np.ndarray]:
        """Drains the queue to get the most recent frame."""
        frame = None
        while not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get_nowait()
            except queue.Empty:
                break
        return frame

    def _live_loop(self):
        """Main loop for sending live MIDI messages."""
        port = None
        while self.running_event.is_set():
            try:
                if port is None:
                    print(f"Opening MIDI port: {self.args.port_name}...")
                    port = mido.open_output(self.args.port_name)
                    print("MIDI port opened.")

                frame = self._get_latest_frame()
                if frame is None:
                    time.sleep(0.01)
                    continue

                # Invert the image for display if requested (bright pixel -> dark LED)
                display_frame = (3 - frame) if self.args.invert else frame

                # --- PWM Cycle ---
                # Recalculate timing each frame to allow for live adjustments
                pwm_timing_ms = [self.args.timing, self.args.timing * 2.0]

                # For each grayscale level, we send a bit-plane.
                # Plane 0 (LSB) is for levels 1 and 3.
                # Plane 1 (MSB) is for levels 2 and 3.
                # We hold each plane on for a specific duration to create the
                # illusion of grayscale.
                bit_planes = [
                    (display_frame & 1).astype(bool),  # LSB
                    ((display_frame >> 1) & 1).astype(bool) # MSB
                ]

                for i, plane in enumerate(bit_planes):
                    if not self.args.pwm and i > 0:
                        break # Skip other planes if PWM is off

                    payload = grid_to_sysex_payload(plane, self.args.device)
                    msg = mido.Message('sysex', data=payload)
                    port.send(msg)
                    time.sleep(pwm_timing_ms[i] / 1000.0)

            except (IOError, mido.MidiError) as e:
                print(f"MIDI Error: {e}. Re-opening in 5 seconds...", file=sys.stderr)
                if port:
                    port.close()
                port = None
                time.sleep(5)
            except Exception:
                print("Error in MIDI sender thread:", file=sys.stderr)
                traceback.print_exc()
                time.sleep(1)

        if port:
            port.close()
        print("MIDI sender thread finished.")

    def _record_loop(self):
        """Main loop for recording MIDI messages to a file."""
        print(f"Recording SysEx output to '{self.args.record}'")
        midi_file = mido.MidiFile(type=0) # Single track file
        track = mido.MidiTrack()
        midi_file.tracks.append(track)

        last_frame_time = time.perf_counter()

        while self.running_event.is_set():
            frame = self._get_latest_frame()
            if frame is None:
                time.sleep(0.01)
                continue

            # Invert frame for recording if needed
            display_frame = (3 - frame) if self.args.invert else frame

            bit_planes = [
                (display_frame & 1).astype(bool),
                ((display_frame >> 1) & 1).astype(bool)
            ]

            # In record mode, time delta is crucial.
            now = time.perf_counter()
            delta_s = now - last_frame_time
            last_frame_time = now

            delta_ticks = mido.second2tick(delta_s, midi_file.ticks_per_beat, 500000) # 120bpm

            for i, plane in enumerate(bit_planes):
                if not self.args.pwm and i > 0:
                    break

                payload = grid_to_sysex_payload(plane, self.args.device)
                msg = mido.Message('sysex', data=payload)

                # Set delta time for the first message of the frame
                msg.time = delta_ticks if i == 0 else 0
                track.append(msg)

        try:
            print("Saving MIDI file...")
            midi_file.save(self.args.record)
            print(f"Successfully saved to '{self.args.record}'")
        except Exception:
            print(f"Error saving MIDI file:", file=sys.stderr)
            traceback.print_exc()
        print("MIDI recorder thread finished.")

# --- GUI ---

class PreviewWindow:
    """A Tkinter window to show the 16x16 frames."""
    def __init__(self, root: tk.Tk, args, frame_queue: queue.Queue, running_event: threading.Event):
        self.root = root
        self.args = args
        self.frame_queue = frame_queue
        self.running_event = running_event
        self.photo = None
        self.canvas_img = None

        self.root.title("16x16 Preview")
        self.canvas = tk.Canvas(root, width=160, height=160, bg="black")
        self.canvas.pack()

        # Handle window close event and key presses
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<KeyPress>", self.key_press_handler)
        self.update_preview()

    def update_preview(self):
        if not self.running_event.is_set():
            try:
                self.root.destroy()
            except tk.TclError:
                pass # Window might already be gone
            return

        frame = None
        while not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get_nowait()
            except queue.Empty:
                break

        if frame is not None:
            # Convert 4-level grid to an 8-bit image for display
            img_8bit = (frame.astype(np.float32) * (255.0 / 3.0)).astype(np.uint8)
            pil_img = Image.fromarray(img_8bit, "L")
            # Enlarge for visibility without blurring
            pil_img = pil_img.resize((160, 160), Image.Resampling.NEAREST)

            self.photo = ImageTk.PhotoImage(pil_img)
            if self.canvas_img is None:
                self.canvas_img = self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
            else:
                self.canvas.itemconfig(self.canvas_img, image=self.photo)

        self.root.after(33, self.update_preview) # ~30fps update

    def on_close(self):
        print("Preview window closed. Shutting down.")
        self.running_event.clear()

    def key_press_handler(self, event):
        """Handles live adjustment of parameters via keyboard."""
        key = event.keysym
        # --- PWM Timing ---
        if key == 'bracketright':  # ]
            self.args.timing = round(self.args.timing + 1.0, 1)
            print(f"PWM Timing: {self.args.timing:.1f} ms")
        elif key == 'bracketleft':  # [
            self.args.timing = round(max(1.0, self.args.timing - 1.0), 1)
            print(f"PWM Timing: {self.args.timing:.1f} ms")
        # --- Contrast Range ---
        elif key == 'plus' or key == 'equal':  # +/=
            self.args.contrast_low = min(49.5, self.args.contrast_low + 0.5)
            self.args.contrast_high = max(50.5, self.args.contrast_high - 0.5)
            print(f"Contrast Range: {self.args.contrast_low:.1f}% - {self.args.contrast_high:.1f}%")
        elif key == 'minus':  # -
            self.args.contrast_low = max(0.0, self.args.contrast_low - 0.5)
            self.args.contrast_high = min(100.0, self.args.contrast_high + 0.5)
            print(f"Contrast Range: {self.args.contrast_low:.1f}% - {self.args.contrast_high:.1f}%")
        # --- Contrast Shift ---
        elif key == 'greater' or key == 'period':  # >/.
            self.args.contrast_low = min(98.0, self.args.contrast_low + 0.5)
            self.args.contrast_high = min(100.0, self.args.contrast_high + 0.5)
            print(f"Contrast Range: {self.args.contrast_low:.1f}% - {self.args.contrast_high:.1f}%")
        elif key == 'less' or key == 'comma':  # </,
            self.args.contrast_low = max(0.0, self.args.contrast_low - 0.5)
            self.args.contrast_high = max(2.0, self.args.contrast_high - 0.5)
            print(f"Contrast Range: {self.args.contrast_low:.1f}% - {self.args.contrast_high:.1f}%")

# --- Main Execution ---

def main():
    """Parses arguments and starts the application threads."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)

    # MIDI arguments
    parser.add_argument("--port", type=str, help="MIDI output port name. If not specified, the first available port will be used.")
    parser.add_argument("--record", type=str, metavar="FILE", help="Record SysEx output to a MIDI file instead of a live port.")
    parser.add_argument("--device", type=int, default=0x10, help="Roland GS device ID (default: 16 / 0x10).")

    # Capture arguments
    parser.add_argument("--monitor", type=int, default=1, help="Monitor to capture (1 = primary, 2 = secondary, etc.).")
    parser.add_argument("--fps", type=int, default=60, help="Target capture frames per second (default: 60).")
    parser.add_argument("--contrast-low", type=float, default=2.0, help="Low percentile for auto-contrast (default: 2.0).")
    parser.add_argument("--contrast-high", type=float, default=98.0, help="High percentile for auto-contrast (default: 98.0).")

    # Display arguments
    parser.add_argument("--pwm", action=argparse.BooleanOptionalAction, default=True, help="Enable 4-level PWM for grayscale (default).")
    parser.add_argument("--timing", type=float, default=20.0, help="Base time in ms for the LSB plane of PWM (default: 20.0).")
    parser.add_argument("--invert", action=argparse.BooleanOptionalAction, default=True, help="Invert output (bright pixel -> dark LED). Use --no-invert for direct mapping.")

    args = parser.parse_args()

    # --- Port Selection ---
    if not args.record:
        try:
            ports = mido.get_output_names()
            if not ports:
                print("No MIDI output ports found!", file=sys.stderr)
                return

            if args.port is None:
                # No port specified, use the first available port as a default.
                args.port_name = ports[0]
                print(f"No MIDI port specified. Defaulting to: '{args.port_name}'")
                print("Use the --port argument to select a different one.")
            elif args.port not in ports:
                # A port was specified, but it's not valid.
                print(f"\nError: Port '{args.port}' not found.", file=sys.stderr)
                print("Available MIDI output ports:")
                for p in ports:
                    print(f"  - '{p}'")
                return
            else:
                # A valid port was specified.
                args.port_name = args.port
        except Exception as e:
            print(f"Could not list MIDI ports: {e}", file=sys.stderr)
            print("Please ensure you have a MIDI backend like 'rtmidi' installed.", file=sys.stderr)
            return

    # --- Setup ---
    # Shared resources for threads
    # Using multiple queues to prevent one slow consumer from blocking others.
    preview_queue = queue.Queue(maxsize=2)
    midi_queue = queue.Queue(maxsize=2)

    running_event = threading.Event()
    running_event.set()

    # --- Start Threads ---
    # The capture thread produces frames and puts them into two queues
    # so the preview and MIDI sender can consume them independently.
    def distributor(in_q, out_qs):
        while running_event.is_set():
            try:
                frame = in_q.get(timeout=1.0)
                for q in out_qs:
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        pass # Consumer is lagging, drop frame
            except queue.Empty:
                continue

    capture_out_queue = queue.Queue(maxsize=2)
    capture = CaptureThread(args, capture_out_queue, running_event)
    distributor_thread = threading.Thread(
        target=distributor,
        args=(capture_out_queue, [preview_queue, midi_queue]),
        daemon=True
    )
    sender = MidiSenderThread(args, midi_queue, running_event)

    print("Starting threads...")
    print("""
    --- Controls (while preview window is focused) ---
    PWM Timing:      [ (faster)    ] (slower)
    Contrast Width:  - (wider)     + (narrower)
    Contrast Shift:  , (darker)    . (brighter)
    ----------------------------------------------------
    """)
    capture.start()
    distributor_thread.start()
    sender.start()

    # --- Start GUI (Main Thread) ---
    root = tk.Tk()
    app = PreviewWindow(root, args, preview_queue, running_event)

    try:
        print("Application started. Close the preview window or press Ctrl+C to exit.")
        root.mainloop()
    except KeyboardInterrupt:
        print("\nCtrl+C detected. Shutting down...")
        running_event.clear()

    # --- Shutdown ---
    running_event.clear()
    print("Waiting for threads to finish...")
    capture.join(timeout=2)
    sender.join(timeout=5)
    print("Exited.")

if __name__ == "__main__":
    main()

