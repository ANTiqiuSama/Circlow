"""Eased tweens on the Tk event loop for the settings window.

One Motion per Tk root drives every running tween from a single ~60 fps
timer. Starting a key that is already running replaces it, and `to` picks up
from where the value is now, so a quick second click or wheel turn turns the
motion around instead of jumping. Windows' "Animation effects" switch
(Accessibility > Visual effects) turns every tween into an instant change."""
from __future__ import annotations

import sys
import time

FRAME_MS = 15


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_out_back(t: float, overshoot: float = 1.2) -> float:
    """Ease out past the end and settle back: a light spring."""
    t -= 1
    return 1 + (overshoot + 1) * t ** 3 + overshoot * t ** 2


def ease_in_out(t: float) -> float:
    return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2


def lerp(start, end, t: float):
    if isinstance(start, tuple):
        return tuple(a + (b - a) * t for a, b in zip(start, end))
    return start + (end - start) * t


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def blend(start: str, end: str, t: float) -> str:
    """A hex colour between two hex colours."""
    a, b = _rgb(start), _rgb(end)
    return "#{:02x}{:02x}{:02x}".format(*(round(x + (y - x) * t) for x, y in zip(a, b)))


def animations_enabled() -> bool:
    """Windows' own animation switch, read only; on elsewhere."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        value = ctypes.c_int(1)
        # SPI_GETCLIENTAREAANIMATION
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except (AttributeError, OSError):
        pass
    return True


def keep_unchanged_widgets(widget) -> bool:
    """Stop Windows repainting every widget whenever the window size changes.

    Tk registers its window classes with CS_HREDRAW | CS_VREDRAW, so a resize
    invalidates the whole window and each label redraws its text even when
    it did not move. Without them only resized widgets and newly exposed
    strips are painted. This changes this process's own Tk window classes
    (TkTopLevel, TkChild), nothing system-wide."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        get, put = user32.GetClassLongPtrW, user32.SetClassLongPtrW
        get.restype = put.restype = ctypes.c_size_t
        get.argtypes = (ctypes.c_void_p, ctypes.c_int)
        put.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t)
        handle = widget.winfo_id()
        for window in (user32.GetParent(handle), handle):
            if window:
                put(window, -26, get(window, -26) & ~0x3)        # GCL_STYLE; CS_VREDRAW | CS_HREDRAW
        return True
    except (AttributeError, OSError):
        return False


def dark_title_bar(widget, caption: str, text: str, border: str) -> bool:
    """Draw this window's title bar (with its minimise / maximise / close
    buttons) dark, in the window's own colours, instead of Windows' white.
    Sets attributes of this window only (DWM), no system setting."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        dwm, user32 = ctypes.windll.dwmapi, ctypes.windll.user32
        handle = widget.winfo_id()
        window = user32.GetParent(handle) or handle

        def put(attribute, value):
            value = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(window, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0
        # DWMWA_USE_IMMERSIVE_DARK_MODE: 20, or 19 on Windows 10 before 20H1.
        dark = put(20, 1) or put(19, 1)
        for attribute, color in ((35, caption), (36, text), (34, border)):   # Windows 11: caption, text, border
            red, green, blue = _rgb(color)
            put(attribute, red | green << 8 | blue << 16)
        return dark
    except (AttributeError, OSError):
        return False


class Motion:
    def __init__(self, root, enabled: bool | None = None):
        self.root = root
        self.enabled = animations_enabled() if enabled is None else enabled
        self.tweens: dict = {}          # key -> [started, duration, step, ease, done]
        self.values: dict = {}          # key -> value a running `to` has reached
        self.job = None

    def run(self, key, duration: float, step, ease=ease_out, done=None) -> None:
        """Call step(eased fraction) every frame for `duration` seconds."""
        if not self.enabled or duration <= 0:
            self.tweens.pop(key, None)
            step(1.0)
            if done:
                done()
            return
        self.tweens[key] = [time.perf_counter(), duration, step, ease, done]
        if self.job is None:
            self.job = self.root.after(1, self._tick)

    def to(self, key, start, end, duration: float, apply, ease=ease_out, done=None) -> None:
        """Move a number (or tuple of numbers) to `end`; a running move on the
        same key continues from its current value rather than from `start`."""
        start = self.values.get(key, start) if key in self.tweens else start

        def step(fraction):
            value = lerp(start, end, fraction)
            self.values[key] = value
            apply(value)

        def finished():
            self.values.pop(key, None)
            if done:
                done()
        self.run(key, duration, step, ease, finished)

    def busy(self, key) -> bool:
        return key in self.tweens

    def target(self, key, default=None):
        """Where a running `to` is right now (its current value)."""
        return self.values.get(key, default)

    def cancel(self, key) -> None:
        self.tweens.pop(key, None)
        self.values.pop(key, None)

    def _tick(self) -> None:
        self.job = None
        now = time.perf_counter()
        for key, tween in list(self.tweens.items()):
            if self.tweens.get(key) is not tween:
                continue
            started, duration, step, ease, done = tween
            fraction = min(1.0, (now - started) / duration)
            try:
                step(ease(fraction))
            except Exception:
                self.tweens.pop(key, None)
                raise
            if fraction >= 1.0 and self.tweens.get(key) is tween:
                del self.tweens[key]
                if done:
                    done()
        if self.tweens:
            spent = (time.perf_counter() - now) * 1000
            self.job = self.root.after(max(1, round(FRAME_MS - spent)), self._tick)


class Throttle:
    """Run `action` at most once per `gap_ms` while calls keep coming, and
    once more after the last call, so a live resize is followed promptly and
    always ends on the final size."""
    def __init__(self, root, gap_ms: int, action):
        self.root, self.gap, self.action = root, gap_ms, action
        self.job = None
        self.again = False

    def __call__(self, *_):
        if self.job is not None:
            self.again = True
            return
        self.action()
        self.job = self.root.after(self.gap, self._release)

    def _release(self):
        self.job = None
        if self.again:
            self.again = False
            self()


class CharWidths:
    """Text width as the sum of cached per-character widths. A Tk font measure
    is a round trip into Tcl; wrapping a page re-measured every prefix of every
    line on each resize event. CJK has no kerning, and the Latin error is a
    pixel or two over a line, well inside the wrap margin."""
    def __init__(self, font):
        self.font = font
        self.widths: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        widths, total = self.widths, 0
        for char in text:
            width = widths.get(char)
            if width is None:
                width = widths[char] = self.font.measure(char)
            total += width
        return total
