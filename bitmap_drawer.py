"""
Bitmap Drawer v0.3
- Слайдеры с подписями значений
- Белый фон при повороте
- Прокручиваемая панель управления
- Зум превью
- 3 режима рисования: snake / smart / click
ESC — аварийная остановка.
"""
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from collections import defaultdict

from PIL import Image, ImageTk, ImageOps, ImageEnhance, ImageFilter
from pynput.mouse import Button, Controller as MouseController
from pynput import keyboard

try:
    import numpy as np
except ImportError:
    print("Требуется numpy: pip install numpy")
    sys.exit(1)


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

        if sys.platform == "win32":
            try:
                from ctypes import windll
                u = windll.user32
                vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
                vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
            except Exception:
                vx = vy = 0
                vw = top.winfo_screenwidth(); vh = top.winfo_screenheight()
        else:
            vx = vy = 0
            vw = top.winfo_screenwidth(); vh = top.winfo_screenheight()

        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        try:
            top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        top.configure(bg="#000000")

        canvas = tk.Canvas(top, bg="#000000", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(vw // 2, 60,
                           text="Растяни прямоугольник по области холста  •  ESC — отмена",
                           fill="#ffffff", font=("Arial", 20, "bold"))

        st = {"x0": 0, "y0": 0, "rect": None}

        def on_press(e):
            st["x0"], st["y0"] = e.x, e.y
            if st["rect"]:
                canvas.delete(st["rect"])
            st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                  outline="#ff3b30", width=3)

        def on_drag(e):
            if not st["rect"]:
                return
            canvas.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)
            w = abs(e.x - st["x0"]); h = abs(e.y - st["y0"])
            canvas.delete("size")
            canvas.create_text((st["x0"] + e.x) // 2, (st["y0"] + e.y) // 2,
                               text=f"{w}×{h}", fill="#ffff00",
                               font=("Arial", 14, "bold"), tags="size")

        def on_release(e):
            x1, y1, x2, y2 = st["x0"], st["y0"], e.x, e.y
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

        top.deiconify(); top.lift(); top.update_idletasks(); top.update()
        top.focus_force()
        try:
            top.grab_set()
        except tk.TclError:
            pass

        self.parent.wait_window(top)
        return self.area


# ============================================================================
#  Планировщики траектории
# ============================================================================
def _find_runs(matrix):
    """Все горизонтальные runs: список (y, x_start, x_end)."""
    H, W = len(matrix), len(matrix[0])
    runs = []
    for y in range(H):
        x = 0
        while x < W:
            if matrix[y][x]:
                xs = x
                while x < W and matrix[y][x]:
                    x += 1
                runs.append((y, xs, x - 1))
            else:
                x += 1
    return runs


def plan_snake(matrix):
    """Змейка: построчно, чередуя направление. Каждый run = отдельный штрих."""
    H, W = len(matrix), len(matrix[0])
    strokes = []
    for y in range(H):
        runs = []
        x = 0
        while x < W:
            if matrix[y][x]:
                xs = x
                while x < W and matrix[y][x]:
                    x += 1
                runs.append((xs, x - 1))
            else:
                x += 1
        if y % 2 == 1:
            runs.reverse()
        for xs, xe in runs:
            strokes.append([(xs, y), (xe, y)])
    return strokes


def plan_smart(matrix):
    """
    Жадный TSP по runs + вертикальная склейка.
    Возвращает список полилиний (каждая — один непрерывный штрих мышью).
    """
    runs = _find_runs(matrix)
    if not runs:
        return []
    # Защита от O(N²): для огромных изображений падаем на snake
    if len(runs) > 2500:
        return plan_snake(matrix)

    H = len(matrix)
    row_runs = defaultdict(list)
    for i, (y, xs, xe) in enumerate(runs):
        row_runs[y].append((xs, xe, i))

    visited = [False] * len(runs)

    def find_unvisited_at(y, x):
        for xs, xe, i in row_runs.get(y, []):
            if not visited[i] and xs <= x <= xe:
                return i
        return None

    def nearest_unvisited(x, y):
        best_i, best_d = -1, None
        for i, (ry, rxs, rxe) in enumerate(runs):
            if visited[i]:
                continue
            dx = 0 if rxs <= x <= rxe else min(abs(x - rxs), abs(x - rxe))
            d = dx + abs(ry - y)
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        return best_i

    strokes = []
    cx, cy = 0, 0
    while True:
        start_i = nearest_unvisited(cx, cy)
        if start_i == -1:
            break
        y, xs, xe = runs[start_i]
        visited[start_i] = True
        stroke = [(xs, y), (xe, y)]
        cx, cy = xe, y

        # цепляем соседние строки, пока можем
        while True:
            next_i = None
            for ny in (cy + 1, cy - 1):
                if 0 <= ny < H:
                    cand = find_unvisited_at(ny, cx)
                    if cand is not None:
                        next_i = cand
                        break
            if next_i is None:
                break
            ny, nxs, nxe = runs[next_i]
            visited[next_i] = True
            stroke.append((cx, ny))           # вертикальный ход
            # идём в дальний конец — так покрываем весь run
            if abs(cx - nxe) >= abs(cx - nxs):
                stroke.append((nxe, ny))
                cx = nxe
            else:
                stroke.append((nxs, ny))
                cx = nxs
            cy = ny

        strokes.append(stroke)
    return strokes


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

    # ---------- click ----------
    def draw_click(self, matrix, area, delay, on_progress=None):
        x0, y0, w, h = area
        H, W = len(matrix), len(matrix[0])
        cw, ch = w / W, h / H
        self._start_listener()
        try:
            total = H
            for y in range(H):
                if self.stop_flag:
                    break
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
                if on_progress:
                    on_progress((y + 1) / total)
        finally:
            self._stop_listener()

    # ---------- polyline (snake / smart) ----------
    def draw_polyline(self, strokes, W, H, area, delay, on_progress=None):
        x0, y0, w, h = area
        cw, ch = w / W, h / H
        step_delay = max(delay * 0.3, 0.0005)

        total_pts = sum(len(s) for s in strokes) or 1
        done = 0

        self._start_listener()
        try:
            for stroke in strokes:
                if self.stop_flag:
                    break
                px, py = stroke[0]
                sx = int(x0 + (px + 0.5) * cw)
                sy = int(y0 + (py + 0.5) * ch)
                sx, sy = self._clamp(sx, sy, x0, y0, w, h)

                self.mouse.position = (sx, sy)
                time.sleep(delay)
                self.mouse.press(Button.left)
                time.sleep(delay * 0.5)

                for i in range(1, len(stroke)):
                    if self.stop_flag:
                        break
                    nx, ny = stroke[i]
                    nsx = int(x0 + (nx + 0.5) * cw)
                    nsy = int(y0 + (ny + 0.5) * ch)
                    nsx, nsy = self._clamp(nsx, nsy, x0, y0, w, h)
                    self._drag_to(sx, sy, nsx, nsy, step_delay)
                    sx, sy = nsx, nsy
                    done += 1
                    if on_progress:
                        on_progress(min(1.0, done / total_pts))

                self.mouse.release(Button.left)
                time.sleep(delay)
                done += 1
        finally:
            self._stop_listener()

    def _drag_to(self, x1, y1, x2, y2, step_delay):
        dx, dy = x2 - x1, y2 - y1
        steps = max(abs(dx), abs(dy))
        if steps == 0:
            return
        for i in range(1, steps + 1):
            if self.stop_flag:
                return
            t = i / steps
            x = int(round(x1 + dx * t))
            y = int(round(y1 + dy * t))
            self.mouse.position = (x, y)
            time.sleep(step_delay)


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
#  Прокручиваемая панель
# ============================================================================
class ScrollFrame(ttk.Frame):
    def __init__(self, parent, width=320):
        super().__init__(parent, width=width)
        self.pack_propagate(False)

        style = ttk.Style()
        bg = style.lookup("TFrame", "background") or "#f0f0f0"
        canvas = tk.Canvas(self, highlightthickness=0, bg=bg)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(canvas)
        self._win = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas = canvas

        def _cfg(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(self._win, width=canvas.winfo_width())

        self.inner.bind("<Configure>", _cfg)
        canvas.bind("<Configure>", _cfg)

        # колесо мыши по всей панели (кроме слайдеров)
        self._bind_wheel(self.inner, canvas)

    def _bind_wheel(self, widget, canvas):
        if not isinstance(widget, ttk.Scale):
            widget.bind("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        for c in widget.winfo_children():
            self._bind_wheel(c, canvas)


# ============================================================================
#  Главное окно
# ============================================================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bitmap Drawer v0.3")
        self.root.geometry("1220x820")
        self.root.minsize(900, 600)

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
    def _labeled_slider(self, parent, label, var, lo, hi, fmt="{:.0f}"):
        """Слайдер с подписью значения справа от метки."""
        head = ttk.Frame(parent)
        head.pack(fill="x", padx=4, pady=(8, 0))
        ttk.Label(head, text=label).pack(side="left")
        val_lbl = ttk.Label(head, text=fmt.format(var.get()),
                            foreground="#3d8bff",
                            font=("Segoe UI", 9, "bold"))
        val_lbl.pack(side="right")

        scale = ttk.Scale(parent, from_=lo, to=hi, variable=var)
        scale.pack(fill="x", padx=4)

        def refresh(*_):
            try:
                val_lbl.config(text=fmt.format(var.get()))
            except (tk.TclError, ValueError, TypeError):
                pass
            self._update_bitmap()

        var.trace_add("write", refresh)
        return scale

    def _labeled_check(self, parent, label, var, on_change=None):
        cb = ttk.Checkbutton(parent, text=label, variable=var,
                             command=on_change or self._update_bitmap)
        cb.pack(anchor="w", padx=4, pady=(2, 0))
        return cb

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ---------------- превью ----------------
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Превью битмапа").pack(anchor="w")
        self.canvas = tk.Canvas(left, bg="#111", highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))
        self.canvas.bind("<Configure>", lambda e: self._render_preview())

        # ---------------- панель управления ----------------
        sf = ScrollFrame(main, width=330)
        sf.pack(side="right", fill="y", padx=(10, 0))
        r = sf.inner

        ttk.Button(r, text="Загрузить изображение…",
                   command=self.load_image).pack(fill="x", padx=4, pady=(4, 0))

        # размеры
        row = ttk.Frame(r); row.pack(fill="x", padx=4, pady=(8, 0))
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

        ttk.Separator(r).pack(fill="x", padx=4, pady=8)

        # ---- фильтры ----
        self.var_rotate = tk.IntVar(value=0)
        self._labeled_slider(r, "Поворот", self.var_rotate, -180, 180, "{:.0f}°")
        self.var_fill_white = tk.BooleanVar(value=False)
        self._labeled_check(r, "Белый фон при повороте", self.var_fill_white)

        self.var_threshold = tk.IntVar(value=128)
        self._labeled_slider(r, "Порог", self.var_threshold, 0, 255, "{:.0f}")

        self.var_brightness = tk.IntVar(value=100)
        self._labeled_slider(r, "Яркость", self.var_brightness, 0, 250, "{:.0f}%")

        self.var_contrast = tk.IntVar(value=100)
        self._labeled_slider(r, "Контраст", self.var_contrast, 0, 250, "{:.0f}%")

        self.var_saturate = tk.IntVar(value=100)
        self._labeled_slider(r, "Насыщенность", self.var_saturate, 0, 250, "{:.0f}%")

        self.var_blur = tk.DoubleVar(value=0.0)
        self._labeled_slider(r, "Размытие", self.var_blur, 0.0, 3.0, "{:.2f} px")

        self.var_sobel = tk.DoubleVar(value=0.0)
        self._labeled_slider(r, "Sobel", self.var_sobel, 0.0, 1.0, "{:.2f}")

        self.var_edges = tk.BooleanVar(value=False)
        self._labeled_check(r, "Усилить края", self.var_edges)
        self.var_dither = tk.BooleanVar(value=False)
        self._labeled_check(r, "Dithering (FS)", self.var_dither)
        self.var_median = tk.BooleanVar(value=False)
        self._labeled_check(r, "Median edges", self.var_median)
        self.var_invert = tk.BooleanVar(value=False)
        self._labeled_check(r, "Инвертировать изображение", self.var_invert)

        ttk.Button(r, text="Сбросить фильтры",
                   command=self._reset_filters).pack(fill="x", padx=4, pady=(8, 0))

        ttk.Separator(r).pack(fill="x", padx=4, pady=8)

        # ---- превью масштаб ----
        ttk.Label(r, text="Превью").pack(anchor="w", padx=4)
        self.var_fit = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Вписать в окно", variable=self.var_fit,
                        command=self._on_fit_toggle).pack(anchor="w", padx=4)
        self.var_zoom = tk.IntVar(value=4)
        self.zoom_lbl = ttk.Label(r, text="", foreground="#3d8bff",
                                  font=("Segoe UI", 9, "bold"))
        # используем slider без вызова _update_bitmap
        head = ttk.Frame(r); head.pack(fill="x", padx=4, pady=(6, 0))
        ttk.Label(head, text="Масштаб").pack(side="left")
        self.zoom_lbl.pack(side="right")
        self.zoom_scale = ttk.Scale(r, from_=1, to=32, variable=self.var_zoom,
                                    command=lambda *_: self._on_zoom_change())
        self.zoom_scale.pack(fill="x", padx=4)

        ttk.Separator(r).pack(fill="x", padx=4, pady=8)

        # ---- режим рисования ----
        ttk.Label(r, text="Режим рисования").pack(anchor="w", padx=4)
        self.var_mode = tk.StringVar(value="smart")
        ttk.Radiobutton(r, text="Click — точка за точкой",
                        variable=self.var_mode, value="click").pack(anchor="w", padx=4)
        ttk.Radiobutton(r, text="Snake — построчно змейкой",
                        variable=self.var_mode, value="snake").pack(anchor="w", padx=4)
        ttk.Radiobutton(r, text="Smart — склеивать линии",
                        variable=self.var_mode, value="smart").pack(anchor="w", padx=4)

        self.var_delay = tk.DoubleVar(value=0.002)
        self._labeled_slider(r, "Задержка", self.var_delay, 0.0005, 0.05,
                             "{:.4f} s")
        # перепривязываем — этот слайдер не должен обновлять битмап
        self.var_delay.trace_add("write",
                                 lambda *_: self.canvas.update_idletasks())

        ttk.Separator(r).pack(fill="x", padx=4, pady=8)

        self.btn_draw = ttk.Button(r, text="Выбрать область и рисовать",
                                   command=self.start_draw)
        self.btn_draw.pack(fill="x", padx=4)
        self.progress = ttk.Progressbar(r, maximum=1.0)
        self.progress.pack(fill="x", padx=4, pady=(8, 0))
        ttk.Label(r,
                  text="ESC — аварийная остановка.\n"
                       "Перед выбором области откройте редактор\n"
                       "и включите инструмент «карандаш 1 px».",
                  foreground="#888", justify="left",
                  wraplength=300).pack(anchor="w", padx=4, pady=(8, 8))

        self._on_fit_toggle()

    # ------------------------------------------------------------- preview UI
    def _on_fit_toggle(self):
        if self.var_fit.get():
            self.zoom_scale.state(["disabled"])
            self.zoom_lbl.config(text="авто")
        else:
            self.zoom_scale.state(["!disabled"])
            self.zoom_lbl.config(text=f"×{int(self.var_zoom.get())}")
        self._render_preview()

    def _on_zoom_change(self):
        if not self.var_fit.get():
            self.zoom_lbl.config(text=f"×{int(self.var_zoom.get())}")
        self._render_preview()

    # ------------------------------------------------------------- filters
    def _reset_filters(self):
        self.var_rotate.set(0)
        self.var_fill_white.set(False)
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

        rot = int(self.var_rotate.get())
        if rot:
            fill = (255, 255, 255) if self.var_fill_white.get() else (0, 0, 0)
            img = img.rotate(-rot, resample=Image.BILINEAR, fillcolor=fill)

        if self.var_invert.get():
            img = ImageOps.invert(img)

        if self.var_brightness.get() != 100:
            img = ImageEnhance.Brightness(img).enhance(self.var_brightness.get() / 100.0)
        if self.var_contrast.get() != 100:
            img = ImageEnhance.Contrast(img).enhance(self.var_contrast.get() / 100.0)
        if self.var_saturate.get() != 100:
            img = ImageEnhance.Color(img).enhance(self.var_saturate.get() / 100.0)

        bl = float(self.var_blur.get())
        if bl > 0.01:
            img = img.filter(ImageFilter.GaussianBlur(bl))

        gray = ImageOps.grayscale(img).resize((W, H), Image.LANCZOS)
        arr = np.asarray(gray, dtype=np.int16)

        if self.var_edges.get():
            k = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]], dtype=np.int32)
            arr = np.clip(_convolve(arr, k), 0, 255).astype(np.int16)

        sw = float(self.var_sobel.get())
        if sw > 0.01:
            kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.int32)
            ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.int32)
            gx = _convolve(arr, kx).astype(np.float32)
            gy = _convolve(arr, ky).astype(np.float32)
            mag = np.clip(np.sqrt(gx * gx + gy * gy), 0, 255)
            arr = np.clip(arr.astype(np.float32) * (1 - sw) + mag * sw,
                          0, 255).astype(np.int16)

        if self.var_dither.get():
            arr = _floyd_steinberg(arr)
        else:
            thr = int(self.var_threshold.get())
            arr = np.where(arr > thr, 255, 0).astype(np.int16)

        if self.var_median.get():
            arr = _median_edge(arr)

        drawn = arr < 128
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

        if self.var_fit.get():
            scale = max(1, min(cw // W, ch // H))
        else:
            scale = max(1, int(self.var_zoom.get()))

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
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{sw // 2 - win.winfo_width() // 2}"
                     f"+{sh // 2 - win.winfo_height() // 2}")
        win.deiconify(); win.lift(); win.focus_force()

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
        matrix = self.bitmap
        H = len(matrix); W = len(matrix[0])

        def on_progress(p):
            self.root.after(0, lambda: self.overlay.set_progress(p))
            self.root.after(0, lambda: self.progress.configure(value=p))

        try:
            if mode == "click":
                self.drawer.draw_click(matrix, area, delay, on_progress)
            else:
                if mode == "snake":
                    strokes = plan_snake(matrix)
                else:
                    strokes = plan_smart(matrix)
                self.drawer.draw_polyline(strokes, W, H, area, delay, on_progress)
        finally:
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