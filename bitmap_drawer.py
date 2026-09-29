"""
Bitmap Drawer — рисует чёрно-белое изображение мышью внутри выбранной области.
ESC — остановить рисование в любой момент.
"""
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk, ImageOps
from pynput.mouse import Button, Controller as MouseController
from pynput import keyboard


# --- DPI-awareness на Windows: чтобы координаты tkinter и pynput совпадали ---
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
#  Выбор области экрана
# ============================================================================
class ScreenAreaSelector:
    """Полноэкранный оверлей, в котором пользователь тянет прямоугольник."""

    def __init__(self, parent):
        self.parent = parent
        self.area = None

    def select(self):
        self.area = None
        top = tk.Toplevel(self.parent)
        top.overrideredirect(True)
        sw = top.winfo_screenwidth()
        sh = top.winfo_screenheight()
        top.geometry(f"{sw}x{sh}+0+0")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.25)
        except tk.TclError:
            pass
        top.configure(bg="black")

        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            sw // 2, 40,
            text="Потяни мышь, чтобы задать область рисования  •  ESC — отмена",
            fill="white", font=("Arial", 18)
        )

        state = {"x0": 0, "y0": 0, "rect": None}

        def on_press(e):
            state["x0"], state["y0"] = e.x_root, e.y_root
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x_root, e.y_root, e.x_root, e.y_root,
                outline="#ff3b30", width=2
            )

        def on_drag(e):
            if state["rect"]:
                canvas.coords(state["rect"], state["x0"], state["y0"],
                              e.x_root, e.y_root)

        def on_release(e):
            x1, y1 = state["x0"], state["y0"]
            x2, y2 = e.x_root, e.y_root
            if abs(x2 - x1) > 5 and abs(y2 - y1) > 5:
                self.area = (min(x1, x2), min(y1, y2),
                             abs(x2 - x1), abs(y2 - y1))
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", lambda e: top.destroy())
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

    # --- ESC-стоп ---
    def _on_key(self, key):
        if key == keyboard.Key.esc:
            self.stop_flag = True
            return False  # снять слушателя

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

    # --- утилиты ---
    @staticmethod
    def _clamp(x, y, x0, y0, w, h):
        x = max(x0, min(x0 + w - 1, x))
        y = max(y0, min(y0 + h - 1, y))
        return x, y

    # --- основное ---
    def draw(self, matrix, area, mode="stroke", delay=0.003, on_progress=None):
        """
        matrix : список строк [H][W] из bool (True = ставим точку)
        area   : (x0, y0, w, h) — экранный прямоугольник
        mode   : 'stroke' | 'click'
        """
        x0, y0, w, h = area
        H = len(matrix)
        W = len(matrix[0])
        cw = w / W
        ch = h / H

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
                else:  # stroke
                    x = 0
                    while x < W and not self.stop_flag:
                        if matrix[y][x]:
                            xs = x
                            while x < W and matrix[y][x]:
                                x += 1
                            xe = x - 1
                            self._stroke(area, cw, ch, xs, y, xe, y, delay)
                        else:
                            x += 1
                if on_progress:
                    on_progress((y + 1) / H)
        finally:
            self._stop_listener()
        return not self.stop_flag

    def _stroke(self, area, cw, ch, xs, ys, xe, ye, delay):
        x0, y0, w, h = area
        sx = int(x0 + (xs + 0.5) * cw)
        sy = int(y0 + (ys + 0.5) * ch)
        ex = int(x0 + (xe + 0.5) * cw)
        ey = int(y0 + (ye + 0.5) * ch)
        sx, sy = self._clamp(sx, sy, x0, y0, w, h)
        ex, ey = self._clamp(ex, ey, x0, y0, w, h)

        self.mouse.position = (sx, sy)
        time.sleep(delay)
        self.mouse.press(Button.left)
        time.sleep(delay)

        dx, dy = ex - sx, ey - sy
        dist = max(abs(dx), abs(dy))
        if dist > 0:
            for i in range(1, dist + 1):
                if self.stop_flag:
                    break
                t = i / dist
                px = int(sx + dx * t)
                py = int(sy + dy * t)
                px, py = self._clamp(px, py, x0, y0, w, h)
                self.mouse.position = (px, py)
                time.sleep(delay * 0.3)

        self.mouse.release(Button.left)
        time.sleep(delay)


# ============================================================================
#  Маленький topmost-индикатор прогресса во время рисования
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
                            font=("Arial", 10), padx=12, pady=6)
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
        self.root.geometry("980x680")
        self.root.minsize(720, 500)

        self.src_image = None
        self.bw_image = None      # PIL "1"
        self.bitmap = None        # matrix [H][W] bool
        self.preview_photo = None
        self.drawing = False

        self.drawer = Drawer()
        self.selector = ScreenAreaSelector(self.root)
        self.overlay = ProgressOverlay(self.root)

        self._build_ui()
        self.root.bind("<Configure>", lambda e: self._render_preview())

    # -------------------------------------------------------------- UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # -------- левая часть: превью --------
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Превью битмапа").pack(anchor="w")
        self.canvas = tk.Canvas(left, bg="#111", highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))

        # -------- правая часть: настройки --------
        right = ttk.Frame(main, width=280)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        ttk.Button(right, text="Загрузить изображение…",
                   command=self.load_image).pack(fill="x")

        # размер
        row = ttk.Frame(right); row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Ширина").pack(side="left")
        self.var_w = tk.IntVar(value=128)
        ttk.Spinbox(row, from_=4, to=1024, textvariable=self.var_w,
                    width=6, command=self._update_bitmap).pack(side="left", padx=4)
        ttk.Label(row, text="Высота").pack(side="left")
        self.var_h = tk.IntVar(value=64)
        ttk.Spinbox(row, from_=4, to=1024, textvariable=self.var_h,
                    width=6, command=self._update_bitmap).pack(side="left", padx=4)
        self.var_w.trace_add("write", lambda *_: self._update_bitmap())
        self.var_h.trace_add("write", lambda *_: self._update_bitmap())

        # порог
        ttk.Label(right, text="Порог").pack(anchor="w", pady=(10, 0))
        self.var_thr = tk.IntVar(value=128)
        ttk.Scale(right, from_=0, to=255, variable=self.var_thr,
                  command=lambda *_: self._update_bitmap()).pack(fill="x")

        # дизеринг / инверсия
        self.var_dither = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="Дизеринг (Floyd–Steinberg)",
                        variable=self.var_dither,
                        command=self._update_bitmap).pack(anchor="w", pady=(4, 0))
        self.var_invert = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="Инвертировать",
                        variable=self.var_invert,
                        command=self._update_bitmap).pack(anchor="w")

        ttk.Separator(right).pack(fill="x", pady=10)

        # режим рисования
        ttk.Label(right, text="Режим рисования").pack(anchor="w")
        self.var_mode = tk.StringVar(value="stroke")
        ttk.Radiobutton(right, text="Stroke — линии (быстро)",
                        variable=self.var_mode,
                        value="stroke").pack(anchor="w")
        ttk.Radiobutton(right, text="Click — точка за точкой (медленно)",
                        variable=self.var_mode,
                        value="click").pack(anchor="w")

        # задержка
        ttk.Label(right, text="Задержка между шагами (сек)").pack(
            anchor="w", pady=(10, 0))
        self.var_delay = tk.DoubleVar(value=0.002)
        ttk.Scale(right, from_=0.0005, to=0.05,
                  variable=self.var_delay).pack(fill="x")

        ttk.Separator(right).pack(fill="x", pady=10)

        self.btn_draw = ttk.Button(right, text="Выбрать область и рисовать",
                                   command=self.start_draw)
        self.btn_draw.pack(fill="x")

        self.progress = ttk.Progressbar(right, maximum=1.0)
        self.progress.pack(fill="x", pady=(8, 0))

        ttk.Label(right,
                  text="ESC — аварийная остановка.\n"
                       "Во время рисования главное окно скрывается.",
                  foreground="#888", justify="left").pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------ image
    def load_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Изображения",
                        "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        try:
            self.src_image = Image.open(path).convert("L")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        self._update_bitmap()

    def _update_bitmap(self, *_):
        if not self.src_image:
            return
        try:
            W = max(4, min(1024, int(self.var_w.get())))
            H = max(4, min(1024, int(self.var_h.get())))
        except (tk.TclError, ValueError, TypeError):
            return

        img = self.src_image.resize((W, H), Image.LANCZOS)

        if self.var_dither.get():
            bw = img.convert("1")  # встроенный FS-дизеринг Pillow
        else:
            thr = int(self.var_thr.get())
            bw = img.point(lambda p: 255 if p > thr else 0).convert("1")

        if self.var_invert.get():
            bw = ImageOps.invert(bw.convert("L")).convert("1")

        self.bw_image = bw
        px = bw.load()
        self.bitmap = [[px[x, y] == 0 for x in range(W)] for y in range(H)]

        self._render_preview()

    def _render_preview(self):
        if not self.bw_image:
            return
        W, H = self.bw_image.size

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 600, 400

        scale = min(cw / W, ch / H)
        scale = max(1, int(scale))
        display = self.bw_image.resize((W * scale, H * scale), Image.NEAREST)
        # .convert("RGB") чтобы фон отображался корректно на канве
        display = display.convert("RGB")
        self.preview_photo = ImageTk.PhotoImage(display)

        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2,
                                 image=self.preview_photo, anchor="center")

    # ------------------------------------------------------------ draw
    def start_draw(self):
        if not self.bitmap:
            messagebox.showinfo("Инфо", "Сначала загрузите изображение.")
            return
        if self.drawing:
            return
        self.drawing = True
        self.root.withdraw()
        self.root.after(200, self._select_and_draw)

    def _select_and_draw(self):
        area = self.selector.select()
        if not area:
            self.drawing = False
            self.root.deiconify()
            return
        self._countdown(3, lambda: self._run_draw(area))

    def _countdown(self, seconds, callback):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#1e1e1e")
        lbl = tk.Label(win, text="", fg="#eee", bg="#1e1e1e",
                       font=("Arial", 40, "bold"), padx=40, pady=20)
        lbl.pack()
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"+{sw // 2 - win.winfo_width() // 2}"
                     f"+{sh // 2 - win.winfo_height() // 2}")

        def tick(n):
            if n <= 0:
                win.destroy()
                callback()
                return
            lbl.config(text=f"Старт через {n}…\nпереключитесь в окно редактора")
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

    # ------------------------------------------------------------ run
    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()