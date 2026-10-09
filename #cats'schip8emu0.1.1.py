import atexit
import array
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import wave
from shutil import which
import tkinter as tk
from tkinter import filedialog, messagebox

# ============================================================
# CHIP-8 Emulator - Tkinter, single file
# ============================================================

WINDOW_W = 600
WINDOW_H = 400
SCALE = 8
SCREEN_W = 64
SCREEN_H = 32
BEEP_HZ = 440.0
BEEP_RATE = 22050
BEEP_VOLUME = 0.28

FONTSET = [
    0xF0,0x90,0x90,0x90,0xF0, 0x20,0x60,0x20,0x20,0x70,
    0xF0,0x10,0xF0,0x80,0xF0, 0xF0,0x10,0xF0,0x10,0xF0,
    0x90,0x90,0xF0,0x10,0x10, 0xF0,0x80,0xF0,0x10,0xF0,
    0xF0,0x80,0xF0,0x90,0xF0, 0xF0,0x10,0x20,0x40,0x40,
    0xF0,0x90,0xF0,0x90,0xF0, 0xF0,0x90,0xF0,0x10,0xF0,
    0xF0,0x90,0xF0,0x90,0x90, 0xE0,0x90,0xE0,0x90,0xE0,
    0xF0,0x80,0x80,0x80,0xF0, 0xE0,0x90,0x90,0x90,0xE0,
    0xF0,0x80,0xF0,0x80,0xF0, 0xF0,0x80,0xF0,0x80,0x80
]

KEYMAP = {
    "1":0x1, "2":0x2, "3":0x3, "4":0xC,
    "q":0x4, "w":0x5, "e":0x6, "r":0xD,
    "a":0x7, "s":0x8, "d":0x9, "f":0xE,
    "z":0xA, "x":0x0, "c":0xB, "v":0xF
}


class AudioEngine:
    """CHIP-8 sound timer beep: continuous square wave while ST > 0."""

    def __init__(self, frequency=BEEP_HZ, sample_rate=BEEP_RATE, volume=BEEP_VOLUME):
        self.frequency = frequency
        self.sample_rate = sample_rate
        self.volume = max(0.0, min(1.0, volume))
        self._muted = False
        self._playing = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._wav_path = self._build_tone_wav()
        atexit.register(self.shutdown)

    def _build_tone_wav(self):
        duration = 0.2
        n_samples = int(self.sample_rate * duration)
        amplitude = int(32767 * self.volume)
        period = max(1.0, self.sample_rate / self.frequency)
        samples = array.array("h")
        for i in range(n_samples):
            samples.append(amplitude if (i % period) < (period * 0.5) else -amplitude)

        fd, path = tempfile.mkstemp(prefix="chip8_beep_", suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(samples.tobytes())
        return path

    @property
    def muted(self):
        return self._muted

    def set_muted(self, muted):
        self._muted = bool(muted)
        if self._muted:
            self.stop()

    def toggle_mute(self):
        self.set_muted(not self._muted)
        return self._muted

    def update(self, sound_timer):
        if not self._muted and sound_timer > 0:
            self.start()
        else:
            self.stop()

    def start(self):
        with self._lock:
            if self._playing:
                return
            self._playing = True
            self._stop.clear()

            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(
                    self._wav_path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP,
                )
                return

            self._thread = threading.Thread(target=self._loop_play, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            if not self._playing:
                return
            self._playing = False
            self._stop.set()

            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            else:
                proc = self._proc
                self._proc = None
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def _loop_play(self):
        player = self._player_cmd()
        if player is None:
            return

        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    player + [self._wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                while self._proc.poll() is None:
                    if self._stop.wait(0.05):
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            self._proc.kill()
                        break
            except OSError:
                break
            finally:
                self._proc = None

    def _player_cmd(self):
        if sys.platform == "darwin":
            return ["afplay"]
        for candidate in ("aplay", "ffplay", "paplay"):
            if which(candidate):
                if candidate == "ffplay":
                    return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
                if candidate == "aplay":
                    return ["aplay", "-q"]
                return [candidate]
        return None

    def shutdown(self):
        self.stop()
        path = self._wav_path
        self._wav_path = None
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


class Chip8:
    def __init__(self):
        self.reset()

    def reset(self):
        self.memory = [0] * 4096
        self.V = [0] * 16
        self.I = 0
        self.pc = 0x200
        self.stack = []
        self.delay = 0
        self.sound = 0
        self.keys = [False] * 16
        self.display = [0] * (SCREEN_W * SCREEN_H)
        self.waiting_key = None
        self.draw_flag = True
        self.memory[0x50:0x50+len(FONTSET)] = FONTSET[:]

    def load_rom(self, data):
        self.reset()
        if len(data) > 4096 - 0x200:
            raise ValueError("ROM is too large for CHIP-8 memory.")
        self.memory[0x200:0x200+len(data)] = data

    def cycle(self):
        if self.waiting_key is not None:
            return

        if self.pc + 1 >= 4096:
            return

        op = (self.memory[self.pc] << 8) | self.memory[self.pc + 1]
        self.pc = (self.pc + 2) & 0xFFF

        nnn = op & 0x0FFF
        nn = op & 0x00FF
        n = op & 0x000F
        x = (op >> 8) & 0xF
        y = (op >> 4) & 0xF

        if op == 0x00E0:
            self.display = [0] * (SCREEN_W * SCREEN_H)
            self.draw_flag = True

        elif op == 0x00EE:
            if self.stack:
                self.pc = self.stack.pop()

        elif op & 0xF000 == 0x1000:
            self.pc = nnn

        elif op & 0xF000 == 0x2000:
            if len(self.stack) < 16:
                self.stack.append(self.pc)
                self.pc = nnn

        elif op & 0xF000 == 0x3000:
            if self.V[x] == nn:
                self.pc += 2

        elif op & 0xF000 == 0x4000:
            if self.V[x] != nn:
                self.pc += 2

        elif op & 0xF00F == 0x5000:
            if self.V[x] == self.V[y]:
                self.pc += 2

        elif op & 0xF000 == 0x6000:
            self.V[x] = nn

        elif op & 0xF000 == 0x7000:
            self.V[x] = (self.V[x] + nn) & 0xFF

        elif op & 0xF00F == 0x8000:
            self.V[x] = self.V[y]

        elif op & 0xF00F == 0x8001:
            self.V[x] |= self.V[y]

        elif op & 0xF00F == 0x8002:
            self.V[x] &= self.V[y]

        elif op & 0xF00F == 0x8003:
            self.V[x] ^= self.V[y]

        elif op & 0xF00F == 0x8004:
            total = self.V[x] + self.V[y]
            self.V[0xF] = 1 if total > 255 else 0
            self.V[x] = total & 0xFF

        elif op & 0xF00F == 0x8005:
            self.V[0xF] = 1 if self.V[x] >= self.V[y] else 0
            self.V[x] = (self.V[x] - self.V[y]) & 0xFF

        elif op & 0xF00F == 0x8006:
            self.V[0xF] = self.V[x] & 1
            self.V[x] >>= 1

        elif op & 0xF00F == 0x8007:
            self.V[0xF] = 1 if self.V[y] >= self.V[x] else 0
            self.V[x] = (self.V[y] - self.V[x]) & 0xFF

        elif op & 0xF00F == 0x800E:
            self.V[0xF] = (self.V[x] >> 7) & 1
            self.V[x] = (self.V[x] << 1) & 0xFF

        elif op & 0xF00F == 0x9000:
            if self.V[x] != self.V[y]:
                self.pc += 2

        elif op & 0xF000 == 0xA000:
            self.I = nnn

        elif op & 0xF000 == 0xB000:
            self.pc = (nnn + self.V[0]) & 0xFFF

        elif op & 0xF000 == 0xC000:
            self.V[x] = random.randint(0, 255) & nn

        elif op & 0xF000 == 0xD000:
            vx = self.V[x] % SCREEN_W
            vy = self.V[y] % SCREEN_H
            self.V[0xF] = 0

            for row in range(n):
                if self.I + row >= 4096:
                    break
                sprite = self.memory[self.I + row]
                for bit in range(8):
                    if sprite & (0x80 >> bit):
                        px = (vx + bit) % SCREEN_W
                        py = (vy + row) % SCREEN_H
                        pos = py * SCREEN_W + px
                        if self.display[pos]:
                            self.V[0xF] = 1
                        self.display[pos] ^= 1

            self.draw_flag = True

        elif op & 0xF0FF == 0xE09E:
            if self.keys[self.V[x] & 0xF]:
                self.pc += 2

        elif op & 0xF0FF == 0xE0A1:
            if not self.keys[self.V[x] & 0xF]:
                self.pc += 2

        elif op & 0xF0FF == 0xF007:
            self.V[x] = self.delay

        elif op & 0xF0FF == 0xF00A:
            self.waiting_key = x

        elif op & 0xF0FF == 0xF015:
            self.delay = self.V[x]

        elif op & 0xF0FF == 0xF018:
            self.sound = self.V[x]

        elif op & 0xF0FF == 0xF01E:
            self.I = (self.I + self.V[x]) & 0xFFF

        elif op & 0xF0FF == 0xF029:
            self.I = 0x50 + (self.V[x] & 0xF) * 5

        elif op & 0xF0FF == 0xF033:
            value = self.V[x]
            self.memory[self.I] = value // 100
            self.memory[self.I + 1] = (value // 10) % 10
            self.memory[self.I + 2] = value % 10

        elif op & 0xF0FF == 0xF055:
            for i in range(x + 1):
                self.memory[self.I + i] = self.V[i]

        elif op & 0xF0FF == 0xF065:
            for i in range(x + 1):
                self.V[i] = self.memory[self.I + i]

    def tick_timers(self):
        if self.delay > 0:
            self.delay -= 1
        if self.sound > 0:
            self.sound -= 1


class App:
    def __init__(self, root):
        self.root = root
        self.vm = Chip8()
        self.audio = AudioEngine()
        self.loaded = False
        self.running = False
        self.last_timer = time.perf_counter()

        root.title("CHIP-8 Emulator")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        root.update_idletasks()
        px = (root.winfo_screenwidth() - WINDOW_W) // 2
        py = (root.winfo_screenheight() - WINDOW_H) // 2
        root.geometry(f"{WINDOW_W}x{WINDOW_H}+{px}+{py}")

        # Blank menu bar: only the standard menu labels, no "No game" text.
        bar = tk.Menu(root)

        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Open ROM...", command=self.open_rom)
        file_menu.add_command(label="Close ROM", command=self.close_rom)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        bar.add_cascade(label="File", menu=file_menu)

        emu_menu = tk.Menu(bar, tearoff=False)
        emu_menu.add_command(label="Run / Pause", command=self.toggle_run)
        emu_menu.add_command(label="Reset", command=self.reset)
        emu_menu.add_separator()
        self.mute_var = tk.BooleanVar(value=False)
        emu_menu.add_checkbutton(
            label="Mute Audio",
            variable=self.mute_var,
            command=self.toggle_mute,
        )
        bar.add_cascade(label="Emulation", menu=emu_menu)

        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(
            label="About",
            command=lambda: messagebox.showinfo(
                "CHIP-8 Emulator",
                "CHIP-8 Emulator\nTkinter 600x400\nAudio: sound-timer square-wave beep",
            )
        )
        bar.add_cascade(label="Help", menu=help_menu)
        root.config(menu=bar)

        self.canvas = tk.Canvas(
            root,
            width=WINDOW_W,
            height=WINDOW_H,
            bg="#0000aa",
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        root.bind("<KeyPress>", self.key_down)
        root.bind("<KeyRelease>", self.key_up)
        root.bind("<Control-o>", lambda e: self.open_rom())
        root.bind("<space>", lambda e: self.toggle_run())

        self.render()
        self.loop()

    def open_rom(self):
        path = filedialog.askopenfilename(
            title="Open CHIP-8 ROM",
            filetypes=[
                ("CHIP-8 ROMs", "*.ch8 *.c8 *.rom"),
                ("All files", "*.*")
            ]
        )
        if not path:
            return

        try:
            with open(path, "rb") as f:
                data = list(f.read())
            self.vm.load_rom(data)
            self.loaded = True
            self.running = True
            self.root.title("CHIP-8 Emulator")
            self.render()
        except Exception as exc:
            messagebox.showerror("CHIP-8 Emulator", str(exc))

    def close_rom(self):
        self.vm.reset()
        self.loaded = False
        self.running = False
        self.audio.update(0)
        self.root.title("CHIP-8 Emulator")
        self.render()

    def reset(self):
        if not self.loaded:
            self.vm.reset()
            self.audio.update(0)
            self.render()
            return
        self.open_rom()

    def toggle_run(self):
        if self.loaded:
            self.running = not self.running
            if not self.running:
                self.audio.update(0)

    def toggle_mute(self):
        self.audio.set_muted(self.mute_var.get())

    def on_close(self):
        self.audio.shutdown()
        self.root.destroy()

    def key_down(self, event):
        key = event.keysym.lower()
        if key in KEYMAP:
            value = KEYMAP[key]
            self.vm.keys[value] = True
            if self.vm.waiting_key is not None:
                self.vm.V[self.vm.waiting_key] = value
                self.vm.waiting_key = None

    def key_up(self, event):
        key = event.keysym.lower()
        if key in KEYMAP:
            self.vm.keys[KEYMAP[key]] = False

    def render(self):
        self.canvas.delete("all")

        # Full blue CHIP-8 display, matching the reference's visual style.
        self.canvas.configure(bg="#0000aa")

        pixel_w = WINDOW_W / SCREEN_W
        pixel_h = WINDOW_H / SCREEN_H

        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                if self.vm.display[y * SCREEN_W + x]:
                    x1 = int(x * pixel_w)
                    y1 = int(y * pixel_h)
                    x2 = int((x + 1) * pixel_w + 1)
                    y2 = int((y + 1) * pixel_h + 1)
                    self.canvas.create_rectangle(
                        x1, y1, x2, y2,
                        fill="#ddffff",
                        outline=""
                    )

        self.vm.draw_flag = False

    def loop(self):
        now = time.perf_counter()

        if self.running and self.loaded:
            # Roughly 700 CHIP-8 instructions/second at a 60 Hz GUI tick.
            for _ in range(12):
                self.vm.cycle()

            if now - self.last_timer >= 1 / 60:
                self.vm.tick_timers()
                self.last_timer = now

            self.audio.update(self.vm.sound)

            if self.vm.draw_flag:
                self.render()
        else:
            self.audio.update(0)

        self.root.after(16, self.loop)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
