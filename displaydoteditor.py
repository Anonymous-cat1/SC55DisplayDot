#!/usr/bin/env python3
"""
SC-55MkII Full 16×16 Editor with MIDI Import & Frame Selection

Draw or import a 16x16 monochrome pattern, export as GS "Data Set 1" SysEx (64 bytes), and send via MIDI.
Requires: Python 3, Tkinter, and optionally 'mido' for MIDI I/O.
"""
import tkinter as tk
from tkinter import filedialog, messagebox
import json

# Attempt to import mido for MIDI functionality
try:
    import mido
    MIDO_AVAILABLE = True
    DEVICE_LIST = mido.get_output_names()
except ImportError:
    MIDO_AVAILABLE = False
    DEVICE_LIST = []

# Roland GS SysEx constants
MANUF_ID = 0x41      # Manufacturer ID
MODEL_ID = 0x45      # SC-55MkII
SUB_ID1 = 0x12       # Data Set 1
SUB_ID2 = 0x10       # Individual Parameter
BLOCK = 0x01         # Displayed Dot Data
MSB = 0x00           # Offset MSB
LSB = 0x10           # Offset LSB

def grid_to_sysex(grid, device_id=0x10):
    data = [0]*64
    for r in range(16):
        for c in range(16):
            if grid[r][c]:
                if c < 5:
                    idx, bit = r, 4-c
                elif c < 10:
                    idx, bit = 16+r, 9-c
                elif c < 15:
                    idx, bit = 32+r, 14-c
                else:
                    idx, bit = 48+r, 19-c
                data[idx] |= (1 << bit)
    
    address = [BLOCK, MSB]
    syx = [0xF0, MANUF_ID, device_id, MODEL_ID, SUB_ID1, SUB_ID2] + address + data
    checksum = ((128 - (sum(address + [LSB] + data) % 128))) & 0x7F # adding LSB only here is a really weird workaround, but works??
    syx += [checksum, 0xF7]
    return syx


def sysex_to_grid(data_bytes):
    grid = [[0]*16 for _ in range(16)]
    for idx, byte in enumerate(data_bytes):
        for bit in range(5):
            if byte & (1 << (4-bit)):
                block = idx // 16
                row = idx % 16
                col = block * 5 + bit
                if col < 16:
                    grid[row][col] = 1  # Set to 1 for "on" state (black)
    return grid


# GUI setup
root = tk.Tk()
root.title("SC-55MkII Display Dot Data Editor")
root.resizable(False, False)
cell_w = 30  # width
cell_h = 10  # height
canvas = tk.Canvas(root, width=16*cell_w, height=16*cell_h, bg='white')

canvas.grid(row=0, column=0, columnspan=5)

grid = [[0]*16 for _ in range(16)]
rects = [[None]*16 for _ in range(16)]
for r in range(16):
    for c in range(16):
        x, y = c*cell_w, r*cell_h
        rects[r][c] = canvas.create_rectangle(x, y, x+cell_w, y+cell_h,
                                              outline='grey', fill='orange')

canvas.bind("<Button-1>", lambda e: toggle_pixel(e))

def toggle_pixel(event):
    c, r = event.x // cell_w, event.y // cell_h
    if 0 <= r < 16 and 0 <= c < 16:
        grid[r][c] ^= 1
        canvas.itemconfig(rects[r][c], fill='black' if grid[r][c] else 'orange')

# MIDI port selector
device_var = tk.StringVar(root)
device_var.set(DEVICE_LIST[0] if DEVICE_LIST else '')
port_label = tk.Label(root, text="MIDI Port:")
port_label.grid(row=1, column=0, sticky='w')
port_menu = tk.OptionMenu(root, device_var, *DEVICE_LIST)
port_menu.config(width=30)
port_menu.grid(row=1, column=1, columnspan=4, sticky='we')

# MIDI Import with selection UI
def import_from_midi():
    if not MIDO_AVAILABLE:
        messagebox.showerror("Error","Install 'mido' to import MIDI.")
        return
    f = filedialog.askopenfilename(filetypes=[('MIDI','*.mid *.midi')])
    if not f:
        return
    mid = mido.MidiFile(f)
    frames = []
    for ti, track in enumerate(mid.tracks):
        for mi, msg in enumerate(track):
            if msg.type == 'sysex':
                d = list(msg.data)
                if (len(d) >= 7 and d[0]==MANUF_ID and d[2]==MODEL_ID \
                   and d[3]==SUB_ID1 and d[4]==SUB_ID2 and d[5]==BLOCK):
                    frames.append((ti, mi, d[7:7+64]))
    if not frames:
        messagebox.showerror("Not found","No SC-55MkII display frames found.")
        return
    # Create a selection window
    sel_win = tk.Toplevel(root)
    sel_win.title("Select Frame to Import")
    tk.Label(sel_win, text="Frames found:").pack(pady=5)
    listbox = tk.Listbox(sel_win, width=30, height=10)
    for idx, (t, m, _) in enumerate(frames):
        listbox.insert(idx, f"Index {idx}: Track {t}, Msg {m}")
    listbox.pack(padx=10)
    def do_import():
        sel = listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        _, _, data_bytes = frames[idx]
        update_grid(sysex_to_grid(data_bytes))
        sel_win.destroy()
        messagebox.showinfo("Imported", f"Imported frame {idx}.")
    tk.Button(sel_win, text="Import Selected", command=do_import).pack(pady=5)

# Other actions

def show_sysex():
    syx = grid_to_sysex(grid)
    hex_string = ' '.join(f"{b:02X}" for b in syx)
    
    # Create popup with copyable text
    popup = tk.Toplevel(root)
    popup.title("SysEx Data")
    tk.Label(popup, text="Copyable SysEx Data:").pack(pady=5)
    text_widget = tk.Text(popup, height=4, width=80)
    text_widget.insert("1.0", hex_string)
    text_widget.pack(padx=10, pady=5)
    text_widget.config(state="normal")
    text_widget.focus()
    text_widget.tag_add("sel", "1.0", "end")  # Select all text
    tk.Button(popup, text="Close", command=popup.destroy).pack(pady=5)


def save_pattern():
    f = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('JSON','*.json')])
    if f:
        with open(f, 'w') as fp:
            json.dump(grid, fp)

def load_pattern():
    f = filedialog.askopenfilename(filetypes=[('JSON','*.json')])
    if f:
        with open(f) as fp:
            data = json.load(fp)
        if len(data)==16 and all(len(r)==16 for r in data):
            update_grid(data)
        else:
            messagebox.showerror("Error","Invalid pattern file.")

def update_grid(new_grid):
    global grid
    grid = new_grid
    for r in range(16):
        for c in range(16):
            canvas.itemconfig(rects[r][c], fill='black' if grid[r][c] else 'orange')

def send_sysex():
    if not MIDO_AVAILABLE:
        messagebox.showerror("Error","Install 'mido' for MIDI output.")
        return
    port = device_var.get()
    if not port:
        messagebox.showerror("Error","Select a MIDI port.")
        return
    with mido.open_output(port) as out:
        out.send(mido.Message('sysex', data=grid_to_sysex(grid)[1:-1]))

def show_help():
    help_win = tk.Toplevel(root)
    help_win.title("Help & Credits")
    help_text = (
        "SC-55MkII Display Dot Data Editor\n\n"
        "Developed by Anonymous_cat1\n"
        "(Actually ChatGPT 4o because nobody bothered to create a spec for Display Dot Data)\n\n"
        "This basic tool allows you to create and send 16x16* images to you SC-55MkII's VU\n"
        "Meter display via SysEx messages.\n\n"
        "Usage:\n"
        "- Draw or import patterns\n"
        "- Save/load patterns as JSON\n"
        "- Import Display Dot Data SysEx from a MIDI file\n"
        "- Send patterns to SC-55MkII via MIDI\n"
        "(Tested with Nuked SC-55)\n\n"
        "Thanks to vg_coder for giving some advice.\n\n"
        "*Pixels on the SC-55's VU meter display are 3:1."
    )
    
    tk.Label(help_win, text=help_text, justify=tk.LEFT, padx=10, pady=10).pack()
    tk.Button(help_win, text="Close", command=help_win.destroy).pack(pady=5)


# Create a frame to hold all buttons in one row
button_frame = tk.Frame(root)
button_frame.grid(row=2, column=0, columnspan=5, sticky='nsew')

# Buttons
buttons = [
    ("Show SysEx", show_sysex),
    ("Save", save_pattern),
    ("Load", load_pattern),
    ("Import MIDI", import_from_midi),
    ("Send", send_sysex),
    ("Help", show_help)
]

# Loop to create buttons dynamically in the same row
for i, (text, cmd) in enumerate(buttons):
    tk.Button(button_frame, text=text, command=cmd).grid(row=0, column=i, padx=5)

# Ensure that the frame doesn't stretch and is centered
root.grid_rowconfigure(2, weight=0)  # Prevent stretching of the button row
root.grid_columnconfigure(0, weight=1)  # Make the first column expand

root.mainloop()
