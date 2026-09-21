#!/usr/bin/env python3
"""
GIF to MIDI SysEx Converter with live playback, looping, contrast adjustment,
and optional merging with an existing MIDI file.

This script reads a GIF file, converts each frame into a 16x16 image, and builds a
corresponding SysEx message using the SC-55MkII display logic. It can either:
  - Write the sequence into a MIDI file,
  - Play the sequence realtime via a specified MIDI output port (optionally looped),
  - Merge the generated MIDI with an existing MIDI file, syncing tempos and durations.

New arguments:
  --play             Play the frames realtime via MIDI output instead of converting to a MIDI file.
  --port PORT        Specify MIDI output port when using --play.
  --threshold N      Set the brightness threshold (0-255) for monochrome conversion (default: 128).
  --invert           Invert the image before thresholding (dark becomes light and vice versa).
  --loop             Loop the realtime playback indefinitely (works with --play).
  --merge-midi FILE  Path to an existing MIDI file to merge with the generated SysEx track.
  --merged-output    Path to save the merged MIDI file (required if --merge-midi is used).

Dependencies:
- Pillow (for image processing)
- mido (for MIDI file creation or realtime output)
- python-rtmidi (for realtime MIDI output)

Usage Examples:
  python gif_to_midi.py input.gif output.mid
  python gif_to_midi.py input.gif --play --port "Your MIDI Port" --threshold 100 --invert
  python gif_to_midi.py input.gif --play --port "Your MIDI Port" --loop
  python gif_to_midi.py input.gif output.mid --merge-midi existing.mid
"""
import sys
import os
import time
import argparse
from PIL import Image, ImageSequence
import mido
from mido import MidiFile, MidiTrack, Message, MetaMessage

# Roland GS SysEx constants
MANUF_ID = 0x41
MODEL_ID = 0x45
SUB_ID1 = 0x12
SUB_ID2 = 0x10
BLOCK = 0x01
MSB = 0x00
LSB = 0x10


def grid_to_sysex(grid, device_id=0x10):
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


def image_to_grid(img, threshold, invert=False):
    if img.mode != 'L':
        img = img.convert('L')
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS
    img = img.resize((16, 16), resample)
    pixels = img.load()
    grid = [[0]*16 for _ in range(16)]
    for r in range(16):
        for c in range(16):
            val = 1 if pixels[c, r] < threshold else 0
            grid[r][c] = 1 - val if invert else val
    return grid


def process_gif_to_sysex(gif_path, threshold, invert):
    if not os.path.exists(gif_path):
        print("Input file not found!")
        sys.exit(1)
    im = Image.open(gif_path)
    frames = []
    for frame in ImageSequence.Iterator(im):
        delay_ms = frame.info.get('duration', 100)
        grid = image_to_grid(frame, threshold, invert)
        syx = grid_to_sysex(grid)
        frames.append((syx, delay_ms))
    return frames


def write_midi(frames, output_midi, ticks_per_beat=480, tempo=500000):
    mid = MidiFile(ticks_per_beat=ticks_per_beat)
    track = MidiTrack()
    mid.tracks.append(track)
    microsec_per_tick = tempo / ticks_per_beat
    def ms_to_ticks(ms): return int((ms * 1000) / microsec_per_tick)
    track.append(MetaMessage('set_tempo', tempo=tempo, time=0))
    for syx, delay_ms in frames:
        delta = ms_to_ticks(delay_ms)
        track.append(Message('sysex', data=syx[1:-1], time=delta))
    track.append(MetaMessage('end_of_track', time=0))
    mid.save(output_midi)
    print(f"MIDI file saved to {output_midi}")


def merge_with_existing_midi(vu_midi_path, existing_midi_path, output_path):
    orig = MidiFile(existing_midi_path)
    tpb = orig.ticks_per_beat
    tempo = next((m.tempo for m in orig.tracks[0] if m.type=='set_tempo'), 500000)
    temp_vu = MidiFile(vu_midi_path)
    merged_vu = MidiTrack()
    microsec_per_tick = tempo / tpb
    def ticks_to_ms(ticks): return (ticks * microsec_per_tick) / 1000
    vu_events = [(ticks_to_ms(msg.time), msg) for msg in temp_vu.tracks[0] if msg.type=='sysex']
    orig_ms = sum(ticks_to_ms(msg.time) for msg in orig.tracks[0])
    acc_ms, idx = 0, 0
    while acc_ms < orig_ms and vu_events:
        dt, msg = vu_events[idx % len(vu_events)]
        dt = min(dt, orig_ms - acc_ms)
        merged_vu.append(msg.copy(time=int((dt * 1000) / microsec_per_tick)))
        acc_ms += dt
        idx += 1
    merged = MidiFile()
    merged.ticks_per_beat = tpb
    for tr in orig.tracks:
        merged.tracks.append(tr.copy())
    merged.tracks.append(merged_vu)
    merged.save(output_path)
    print(f"Merged MIDI saved to {output_path}")


def play_realtime(frames, midi_port, loop=False):
    print(f"Opening MIDI output port: {midi_port}")
    try:
        with mido.open_output(midi_port) as outport:
            if loop:
                print("Realtime playback looping indefinitely. Press Ctrl+C to stop.")
                while True:
                    for syx, delay in frames:
                        outport.send(Message('sysex', data=syx[1:-1]))
                        time.sleep(delay/1000)
            else:
                for syx, delay in frames:
                    outport.send(Message('sysex', data=syx[1:-1]))
                    time.sleep(delay/1000)
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(0)
    except Exception as e:
        print("Playback error:", e)
        sys.exit(1)


def list_midi_ports():
    ports = mido.get_output_names()
    if ports:
        print("Available MIDI Output Ports:")
        for p in ports: print("  ", p)
    else:
        print("No MIDI output ports found.")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Convert a GIF into a MIDI sequence of SysEx messages or play realtime.")
    parser.add_argument("gif", help="Path to the input GIF file")
    parser.add_argument("output", nargs="?", help="Path to output MIDI file (if not playing). If merging, this becomes the final merged output by default.")
    parser.add_argument("--play", action="store_true", help="Play realtime via MIDI output")
    parser.add_argument("--port", type=str, help="MIDI output port name")
    parser.add_argument("--threshold", type=int, default=128,
                        help="Brightness threshold for binary conversion")
    parser.add_argument("--invert", action="store_true", help="Invert image before thresholding")
    parser.add_argument("--loop", action="store_true",
                        help="Loop realtime playback indefinitely")
    parser.add_argument("--merge-midi", type=str,
                        help="Path to existing MIDI to merge with")
    parser.add_argument("--merged-output", type=str,
                        help="Path to save merged MIDI (optional; defaults to <output> if --merge-midi is used)")
    return parser.parse_args()


def main():
    args = parse_arguments()
    if args.merge_midi and not args.merged_output:
        if not args.output:
            print("When using --merge-midi without --merged-output, you must provide <output> for the merged file.")
            sys.exit(1)
        args.merged_output = args.output
    print("Processing GIF frames...")
    frames = process_gif_to_sysex(args.gif, args.threshold, args.invert)
    if not frames:
        print("No frames extracted!")
        sys.exit(1)

    if args.play:
        if not args.port:
            print("Realtime mode requires --port.")
            list_midi_ports()
            sys.exit(1)
        print(f"Playing {len(frames)} frames realtime{' with looping' if args.loop else ''}...")
        play_realtime(frames, args.port, loop=args.loop)
    else:
        if not args.output and not args.merge_midi:
            print("Output MIDI filename required.")
            sys.exit(1)
        if args.merge_midi:
            temp_vu = (args.merged_output or args.output) + ".tmp.mid"
            orig = MidiFile(args.merge_midi)
            tpb = orig.ticks_per_beat
            tempo = next((m.tempo for m in orig.tracks[0] if m.type=='set_tempo'), 500000)
            write_midi(frames, temp_vu, ticks_per_beat=tpb, tempo=tempo)
            merge_with_existing_midi(temp_vu, args.merge_midi, args.merged_output)
            os.remove(temp_vu)
        else:
            write_midi(frames, args.output)
    print("Done!")

if __name__ == '__main__':
    main()
