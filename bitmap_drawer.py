"""
Bitmap Drawer v0.2
Рисует ч/б изображение мышью внутри выбранной области экрана.
ESC — аварийная остановка.
"""
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk, ImageOps, ImageEnhance, ImageFilter
from pynput.mouse import Button, Controller as MouseController
from pynput import keyboard

try:
    import numpy as np
except ImportError:
    print("Требуется numpy: pip install numpy")
    sys.exit(1)


# ---------- DPI-awareness на Windows ----------
if sys.platform == "win32":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ============================================================================
#  Утилиты изображения
# ============================================================================
def _convolve(arr, kernel):
    """Свёртка с edge-паддингом. arr: (H,W) int, kernel: (kh,kw) int."""
    kh, kw = kernel.shape
    ph, pw = kh // 2, kw // 2
    padded = np.pad(arr.astype(np.int32), ((ph, ph), (pw, pw)), mode="edge")
    h, w = arr.shape
    out = np.zeros((h, w), dtype=np.int32)
    for i in range(kh):
        for j in range(kw):
            out += int(kernel[i, j]) * padded[i:i + h, j:j + w]
    return out


def _floyd_steinberg(arr):
    """Порт FS-дизеринга из bitmaper.html."""
    a = arr.astype(np.float32).copy()
    h, w = a.shape
    for y in range(h):
        row = a[y]
        nxt = a[y + 1] if y + 1 < h else None
        for x in range(w):
            old = row[x]
            new = 0.0 if old < 128 else 255.0
            err = old - new
            row[x] = new
            if x + 1 < w:
                row[x + 1] += err * 7 / 16
            if nxt is not None:
                if x > 0:
                    nxt[x - 1] += err * 3 / 16
                nxt[x] += err * 5 / 16
                if x + 1 < w:
                    nxt[x + 1] += err * 1 / 16
    return a.astype(np.int16)


def _median_edge(arr):
    """Range-фильтр как в bitmaper (max−min по соседям self/right/down)."""
    h, w = arr.shape
    out = arr.copy()
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            v0 = arr[y, x]; v1 = arr[y, x + 1]; v2 = arr[y + 1, x]
            out[y, x] = max(v0, v1, v2) - min(v0, v1, v2)
    return out


# ============================================================================
#  Выбор области экрана
# ============================================================================
class ScreenAreaSelector:
    def __init__(self, parent):
        self.parent = parent
        self.area = None

    def select(self):
        self.area = None
        top = tk.Toplevel(self.parent)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)

        # virtual screen (мульти-монитор на Windows)
        if sys.platform == "win32":
            try:
                from ctypes import windll
                u = windll.user32
                vx = u.GetSystemMetrics(76)
                vy = u.GetSystemMetrics(77)
                vw = u.GetSystemMetrics(78)
                vh = u.GetSystemMetrics(79)
            except Exception:
                vx = vy = 0
                vw = top.winfo_screenwidth()
                vh = top.winfo_screenheight()
        else:
            vx = vy = 0
            vw = top.winfo_screenwidth()
            vh = top.winfo_screenheight()

        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        try:
            top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        top.configure(bg="#000000")

        canvas = tk.Canvas(top, bg="#000000", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            vw // 2, 60,
            text="Растяни прямоугольник по области холста  •  ESC — отмена",
            fill="#ffffff", font=("Arial", 20, "bold"), tags="hint",
        )

        state = {"x0": 0, "y0": 0, "rect": None}

        def on_press(e):
            state["x0"], state["y0"] = e.x, e.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#ff3b30", width=3
            )

        def on_drag(e):
            if not state["rect"]:
                return
            canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)
            w = abs(e.x - state["x0"]); h = abs(e.y - state["y0"])
            canvas.delete("size")
            canvas.create_text(
                (state["x0"] + e.x) // 2, (state["y0"] + e.y) // 2,
                text=f"{w}×{h}", fill="#ffff00",
                font=("Arial", 14, "bold"), tags="size",
            )

        def on_release(e):
            x1, y1 = state["x0"], state["y0"]
            x2, y2 = e.x, e.y
            if abs(x2 - x1) > 5 and abs(y2 - y1) > 5:
                self.area = (min(x1, x2) + vx, min(y1, y2) + vy,
                             abs(x2 - x1), abs(y2 - y1))
            _close()

        def _close(*_):
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", _close)
        top.protocol("WM_DELETE_WINDOW", _close)

        top.deiconify()
        top.lift()
        top.update_idletasks()
        top.update()
        top.focus_force()
        try:
            top.grab_set()
        except tk.TclError:
            pass

        self.parent.wait_window(top)
        return self.area


# ============================================================================
#  Рисователь
# ============================================================================
class Drawer:
    def __init__(self):
        self.mouse = MouseController()
        self.stop_flag = False
        self._listener = None

    def _on_key(self, key):
        if key == keyboard.Key.esc:
            self.stop_flag = True
            return False

    def _start_listener(self):
        self.stop_flag = False
        self._listener = keyboard.Listener(on_press=self._on_key)
        self._listener.start()

    def _stop_listener(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    @staticmethod
    def _clamp(x, y, x0, y0, w, h):
        return max(x0, min(x0 + w - 1, x)), max(y0, min(y0 + h - 1, y))

    def draw(self, matrix, area, mode="stroke", delay=0.003, on_progress=None):
        x0, y0, w, h = area
        H = len(matrix)
        W = len(matrix[0])
        cw, ch = w / W, h / H

        self._start_listener()
        try:
            for y in range(H):
                if self.stop_flag:
                    break
                if mode == "click":
                    for x in range(W):
                        if self.stop_flag:
                            break
                        if matrix[y][x]:
                            sx = int(x0 + (x + 0.5) * cw)
                            sy = int(y0 + (y + 0.5) * ch)
                            sx, sy = self._clamp(sx, sy, x0, y0, w, h)
                            self.mouse.position = (sx, sy)
                            time.sleep(delay * 0.5)
                            self.mouse.press(Button.left)
                            time.sleep(delay * 0.5)
                            self.mouse.release(Button.left)
                else:
                    x = 0
                    while x < W and not self.stop_flag:
                        if matrix[y][x]:
                            xs = x
                            while x < W and matrix[y][x]:
                                x += 1
                            self._stroke(area, cw, ch, xs, x - 1, y, delay)
                        else:
                            x += 1
                if on_progress:
                    on_progress((y + 1) / H)
        finally:
            self._stop_listener()
        return not self.stop_flag

    def _stroke(self, area, cw, ch, xs, xe, y, delay):
        x0, y0, w, h = area
        sx = int(x0 + (xs + 0.5) * cw)
        sy = int(y0 + (y + 0.5) * ch)
        ex = int(x0 + (xe + 0.5) * cw)
        ey = sy
        sx, sy = self._clamp(sx, sy, x0, y0, w, h)
        ex, ey = self._clamp(ex, ey, x0, y0, w, h)

        self.mouse.position = (sx, sy)
        time.sleep(delay)
        self.mouse.press(Button.left)
        time.sleep(delay)
        dist = max(abs(ex - sx), abs(ey - sy))
        if dist > 0:
            for i in range(1, dist + 1):
                if self.stop_flag:
                    break
                t = i / dist
                px = int(sx + (ex - sx) * t)
                py = int(sy + (ey - sy) * t)
                px, py = self._clamp(px, py, x0, y0, w, h)
                self.mouse.position = (px, py)
                time.sleep(delay * 0.3)
        self.mouse.release(Button.left)
        time.sleep(delay)


# ============================================================================
#  Прогресс-оверлей
# ============================================================================
class ProgressOverlay:
    def __init__(self, parent):
        self.parent = parent
        self.win = None
        self.lbl = None

    def show(self):
        self.win = tk.Toplevel(self.parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#1e1e1e")
        self.lbl = tk.Label(self.win, text="Рисую…  ESC — стоп",
                            fg="#eee", bg="#1e1e1e",
                            font=("Arial", 11), padx=14, pady=8)
        self.lbl.pack()
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        self.win.geometry(f"+{sw - self.win.winfo_width() - 20}+20")

    def set_progress(self, p):
        if self.lbl:
            self.lbl.config(text=f"Рисую… {int(p * 100)}%  •  ESC — стоп")

    def hide(self):
        if self.win:
            self.win.destroy()
            self.win = None
            self.lbl = None


# ============================================================================
#  Главное окно
# ============================================================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bitmap Drawer")
        self.root.geometry("1100x800")
        self.root.minsize(900, 700)

        self.src_image = None
        self.bw_image = None
        self.bitmap = None
        self.preview_photo = None
        self.drawing = False

        self.drawer = Drawer()
        self.selector = ScreenAreaSelector(self.root)
        self.overlay = ProgressOverlay(self.root)

        self._build_ui()
        self.root.bind("<Configure>", lambda e: self._render_preview())

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # --- превью ---
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Превью битмапа").pack(anchor="w")
        self.canvas = tk.Canvas(left, bg="#111", highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))

        # --- панель управления ---
        right = ttk.Frame(main, width=300)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        ttk.Button(right, text="Загрузить изображение…",
                   command=self.load_image).pack(fill="x")

        # размеры
        row = ttk.Frame(right); row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="W").pack(side="left")
        self.var_w = tk.IntVar(value=128)
        ttk.Spinbox(row, from_=4, to=1024, textvariable=self.var_w,
                    width=6).pack(side="left", padx=2)
        ttk.Label(row, text="H").pack(side="left")
        self.var_h = tk.IntVar(value=64)
        ttk.Spinbox(row, from_=4, to=1024, textvariable=self.var_h,
                    width=6).pack(side="left", padx=2)
        self.var_w.trace_add("write", lambda *_: self._update_bitmap())
        self.var_h.trace_add("write", lambda *_: self._update_bitmap())

        # ---- Фильтры ----
        def slider(parent, label, var, lo, hi, res=1):
            ttk.Label(parent, text=label).pack(anchor="w", pady=(6, 0))
            s = ttk.Scale(parent, from_=lo, to=hi, variable=var,
                          command=lambda *_: self._update_bitmap())
            s.pack(fill="x")
            return s

        def check(parent, label, var):
            ttk.Checkbutton(parent, text=label, variable=var,
                            command=self._update_bitmap).pack(anchor="w")

        ttk.Separator(right).pack(fill="x", pady=8)

        self.var_rotate = tk.IntVar(value=0)
        slider(right, "Поворот", self.var_rotate, -180, 180)
        self.var_threshold = tk.IntVar(value=128)
        slider(right, "Порог", self.var_threshold, 0, 255)
        self.var_brightness = tk.IntVar(value=100)
        slider(right, "Яркость", self.var_brightness, 0, 250)
        self.var_contrast = tk.IntVar(value=100)
        slider(right, "Контраст", self.var_contrast, 0, 250)
        self.var_saturate = tk.IntVar(value=100)
        slider(right, "Насыщенность", self.var_saturate, 0, 250)
        self.var_blur = tk.DoubleVar(value=0.0)
        slider(right, "Размытие", self.var_blur, 0.0, 3.0)
        self.var_sobel = tk.DoubleVar(value=0.0)
        slider(right, "Sobel", self.var_sobel, 0.0, 1.0)

        self.var_edges = tk.BooleanVar(value=False);  check(right, "Усилить края", self.var_edges)
        self.var_dither = tk.BooleanVar(value=False); check(right, "Dithering",    self.var_dither)
        self.var_median = tk.BooleanVar(value=False); check(right, "Median edges", self.var_median)
        self.var_invert = tk.BooleanVar(value=False); check(right, "Инвертировать", self.var_invert)

        ttk.Button(right, text="Сбросить фильтры",
                   command=self._reset_filters).pack(fill="x", pady=(8, 0))

        ttk.Separator(right).pack(fill="x", pady=8)

        # ---- Режим рисования ----
        ttk.Label(right, text="Режим рисования").pack(anchor="w")
        self.var_mode = tk.StringVar(value="stroke")
        ttk.Radiobutton(right, text="Stroke — линии (быстро)",
                        variable=self.var_mode, value="stroke").pack(anchor="w")
        ttk.Radiobutton(right, text="Click — точка за точкой",
                        variable=self.var_mode, value="click").pack(anchor="w")

        ttk.Label(right, text="Задержка (сек)").pack(anchor="w", pady=(6, 0))
        self.var_delay = tk.DoubleVar(value=0.002)
        ttk.Scale(right, from_=0.0005, to=0.05,
                  variable=self.var_delay).pack(fill="x")

        ttk.Separator(right).pack(fill="x", pady=8)

        self.btn_draw = ttk.Button(right, text="Выбрать область и рисовать",
                                   command=self.start_draw)
        self.btn_draw.pack(fill="x")
        self.progress = ttk.Progressbar(right, maximum=1.0)
        self.progress.pack(fill="x", pady=(8, 0))
        ttk.Label(right,
                  text="ESC — аварийная остановка.\n"
                       "Перед выбором области откройте редактор\n"
                       "и включите инструмент «карандаш 1 px».",
                  foreground="#888", justify="left").pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------- filters
    def _reset_filters(self):
        self.var_rotate.set(0)
        self.var_threshold.set(128)
        self.var_brightness.set(100)
        self.var_contrast.set(100)
        self.var_saturate.set(100)
        self.var_blur.set(0.0)
        self.var_sobel.set(0.0)
        self.var_edges.set(False)
        self.var_dither.set(False)
        self.var_median.set(False)
        self.var_invert.set(False)
        self._update_bitmap()

    # ----------------------------------------------------------------- image
    def load_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Изображения",
                        "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        try:
            self.src_image = Image.open(path).convert("RGB")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        self._update_bitmap()

    def _update_bitmap(self, *_):
        if not self.src_image:
            return
        try:
            W = max(4, min(2048, int(self.var_w.get())))
            H = max(4, min(2048, int(self.var_h.get())))
        except (tk.TclError, ValueError, TypeError):
            return

        img = self.src_image.copy()

        # 1) поворот
        rot = self.var_rotate.get()
        if rot:
            img = img.rotate(-rot, resample=Image.BILINEAR, fillcolor=(0, 0, 0))

        # 2) инверсия
        if self.var_invert.get():
            img = ImageOps.invert(img)

        # 3) яркость / контраст / насыщенность
        if self.var_brightness.get() != 100:
            img = ImageEnhance.Brightness(img).enhance(self.var_brightness.get() / 100.0)
        if self.var_contrast.get() != 100:
            img = ImageEnhance.Contrast(img).enhance(self.var_contrast.get() / 100.0)
        if self.var_saturate.get() != 100:
            img = ImageEnhance.Color(img).enhance(self.var_saturate.get() / 100.0)

        # 4) размытие
        bl = float(self.var_blur.get())
        if bl > 0.01:
            img = img.filter(ImageFilter.GaussianBlur(bl))

        # 5) в градации серого и ресайз к размеру битмапа
        gray = ImageOps.grayscale(img).resize((W, H), Image.LANCZOS)
        arr = np.asarray(gray, dtype=np.int16)

        # 6) усиление краёв
        if self.var_edges.get():
            k = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]], dtype=np.int32)
            arr = np.clip(_convolve(arr, k), 0, 255).astype(np.int16)

        # 7) Sobel
        sw = float(self.var_sobel.get())
        if sw > 0.01:
            kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.int32)
            ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.int32)
            gx = _convolve(arr, kx).astype(np.float32)
            gy = _convolve(arr, ky).astype(np.float32)
            mag = np.clip(np.sqrt(gx * gx + gy * gy), 0, 255)
            arr = np.clip(arr.astype(np.float32) * (1 - sw) + mag * sw,
                          0, 255).astype(np.int16)

        # 8) дизеринг или порог
        if self.var_dither.get():
            arr = _floyd_steinberg(arr)
        else:
            thr = int(self.var_threshold.get())
            arr = np.where(arr > thr, 255, 0).astype(np.int16)

        # 9) median edges
        if self.var_median.get():
            arr = _median_edge(arr)

        # 10) собрать изображение и матрицу
        drawn = arr < 128                       # True = рисуем (тёмный пиксель)
        preview_arr = np.where(drawn, 0, 255).astype(np.uint8)
        self.bw_image = Image.fromarray(preview_arr, "L")
        self.bitmap = drawn.tolist()

        self._render_preview()

    def _render_preview(self):
        if not self.bw_image:
            return
        W, H = self.bw_image.size
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 640, 420
        scale = max(1, min(cw // W, ch // H))
        disp = self.bw_image.resize((W * scale, H * scale), Image.NEAREST).convert("RGB")
        self.preview_photo = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2,
                                 image=self.preview_photo, anchor="center")

    # ------------------------------------------------------------------ draw
    def start_draw(self):
        if not self.bitmap:
            messagebox.showinfo("Инфо", "Сначала загрузите изображение.")
            return
        if self.drawing:
            return
        self.drawing = True
        self.root.after(100, self._select_and_draw)

    def _select_and_draw(self):
        area = self.selector.select()
        if not area:
            self.drawing = False
            return
        # только теперь прячем главное окно
        self.root.withdraw()
        self.root.after(200, lambda: self._countdown(3, lambda: self._run_draw(area)))

    def _countdown(self, seconds, callback):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#1e1e1e")
        lbl = tk.Label(win, text="", fg="#eee", bg="#1e1e1e",
                       font=("Arial", 28, "bold"), padx=40, pady=24)
        lbl.pack()
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"+{sw // 2 - win.winfo_width() // 2}"
                     f"+{sh // 2 - win.winfo_height() // 2}")
        win.deiconify()
        win.lift()
        win.focus_force()

        def tick(n):
            if n <= 0:
                win.destroy()
                callback()
                return
            lbl.config(text=f"Старт через {n}…\nПереключитесь в редактор")
            win.after(1000, lambda: tick(n - 1))
        tick(seconds)

    def _run_draw(self, area):
        self.progress["value"] = 0
        self.overlay.show()
        t = threading.Thread(target=self._draw_worker, args=(area,), daemon=True)
        t.start()

    def _draw_worker(self, area):
        delay = float(self.var_delay.get())
        mode = self.var_mode.get()

        def on_progress(p):
            self.root.after(0, lambda: self.overlay.set_progress(p))
            self.root.after(0, lambda: self.progress.configure(value=p))

        self.drawer.draw(self.bitmap, area, mode=mode, delay=delay,
                         on_progress=on_progress)
        self.root.after(0, self._draw_done)

    def _draw_done(self):
        self.overlay.hide()
        self.progress["value"] = 0
        self.drawing = False
        self.root.deiconify()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()