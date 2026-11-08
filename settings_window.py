"""User-opened settings window; secrets stay in the local data directory.

Five pages:
* 概览     live source cards in the tray's order, written like the hover panel,
           the tray-icon check and the last half hour of output;
* 来源     which sources are shown, MiMo's console login / manual plan, and the
           local records each reader uses;
* 模型智商 the panel banner's AI Stupid Level scores and Grok's LiveBench column;
* 任务     herdr and the optional task-naming API;
* 常规     start with Windows, how often each source is read, tools, exit.

There is no save button: a change is saved on its own a moment after it is
made (switches at once, text fields once typing stops, source switches after a
short pause so several flips restart the tray once), and the footer says so.
Each group is saved by itself, so a field that does not check out (shown in
the footer) never holds back the others. Buttons such as 登录 or 立即读取 act at
once and say so next to them.
"""
from __future__ import annotations

import app_identity
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit

import agent_tasks
import settings_view as view
import ui_motion


def picture_factor() -> int:
    """How many times larger than their Tk size pictures are drawn: the
    screen's backing scale on macOS (2 on Retina), where settings_mac lays the
    sharp pictures over Tk's one-pixel-per-point copies; 1 elsewhere."""
    override = os.environ.get("CODEX_QUOTA_PICTURE_SCALE", "").strip()
    if override.isdigit() and int(override) >= 1:
        return int(override)
    if sys.platform != "darwin":
        return 1
    try:
        from AppKit import NSScreen
        screen = NSScreen.mainScreen()
        return max(1, round(float(screen.backingScaleFactor()))) if screen is not None else 1
    except Exception:
        return 1


def load_summary() -> dict:
    try:
        stored = json.loads(agent_tasks.SUMMARY_CONFIG.read_text(encoding="utf-8"))
        return {**{name: str(stored.get(name) or "") for name in ("url", "key", "model")},
                "enabled": stored.get("enabled", True)}
    except (OSError, ValueError, AttributeError):
        return {"url": "", "key": "", "model": "", "enabled": False}


def save_summary(config: dict, enabled: bool) -> None:
    values = {name: str(config.get(name) or "").strip() for name in ("url", "key", "model")}
    if enabled:
        address = urlsplit(values["url"])
        if address.scheme not in ("http", "https") or not address.hostname or not values["model"]:
            raise ValueError("请填写有效的 API 地址与模型名称")
        if agent_tasks.is_mimo_url(values["url"]) and not values["key"]:
            raise ValueError("MiMo 需要填写 API Key")
    path = agent_tasks.SUMMARY_CONFIG.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**values, "enabled": enabled}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


PAGES = (("overview", "概览"), ("sources", "来源"), ("iq", "模型智商"), ("tasks", "任务"), ("general", "常规"))
# Older page names (the panel, the tray menu) land on the page that now holds them.
PAGE_ALIASES = {"mimo": "sources", "agents": "tasks", "naming": "tasks"}
PROVIDER_NAMES = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude Code", "mimo": "Xiaomi MiMo"}
NOTICE_SECONDS = 6
# Milliseconds from the last change to the automatic save: a switch at once,
# a text field once typing stops, source switches after a pause (each save
# of them restarts the tray, so several flips are applied together).
SAVE_DELAYS = {"switch": 150, "text": 800, "source": 1200}


def save_delay(keys) -> int:
    """The wait before saving these changed staged keys (see SAVE_DELAYS)."""
    keys = list(keys)
    if any(key.startswith("source:") for key in keys):
        return SAVE_DELAYS["source"]
    if any(key.startswith(("field:", "mimo:")) or key == "iq:key" for key in keys):
        return SAVE_DELAYS["text"]
    return SAVE_DELAYS["switch"]
_LATIN = re.compile(r"[A-Za-z0-9_./:@%+-]")


def wrap_text(text: str, measure, width: int) -> str:
    """Line breaks for mixed Chinese / Latin text. Tk wraps only at spaces, so a
    space inside Chinese ("每 3 小时") ends the line there and pushes a whole
    clause down. Break by character instead; a Latin word stays whole when it
    can move to the next line."""
    lines = []
    for paragraph in (text or "").split("\n"):
        line, used = "", 0
        for char in paragraph:
            step = measure(char)
            if not line or used + step <= width:
                line += char
                used += step
                continue
            cut = len(line)
            if _LATIN.match(char):
                while cut > 0 and _LATIN.match(line[cut - 1]):
                    cut -= 1
                if cut < len(line) * .5:          # one very long word: break it
                    cut = len(line)
            lines.append(line[:cut].rstrip())
            line = (line[cut:] + char).lstrip()
            used = measure(line)
        lines.append(line)
    return "\n".join(lines)


def round_points(x0, y0, x1, y1, radius) -> tuple:
    """Polygon points that Tk's smooth=True turns into a rounded rectangle."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    return (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)


def flow_rows(width: float, widths, gap: float) -> list[list[int]]:
    """Fixed-width tiles laid left to right, wrapping when the next one does
    not fit, like the cards in Windows Settings: a wider window takes three,
    four, five a row until they all sit on one line. Every row keeps at
    least one tile."""
    rows, used = [], None
    for index, size in enumerate(widths):
        if used is not None and used + gap + size <= width + .5:
            rows[-1].append(index)
            used += gap + size
        else:
            rows.append([index])
            used = size
    return rows


def flow_spots(width: float, widths, heights_for, gap: float):
    """(rows, [(x, y, tile width)], total height, widest row) for fixed-width
    tiles. `heights_for(rows)` gives each tile's height once the rows are
    known (a card is as tall as the tallest card beside it). Tiles wider
    than the page are narrowed to it; nothing is stretched."""
    widths = [min(size, width) for size in widths]
    rows = flow_rows(width, widths, gap)
    heights = heights_for(rows)
    spots = [None] * len(widths)
    top, widest = 0.0, 0.0
    for row in rows:
        left = 0.0
        for index in row:
            spots[index] = (left, top, widths[index])
            left += widths[index] + gap
        widest = max(widest, left - gap)
        top += max(heights[index] for index in row) + gap
    return rows, spots, max(0.0, top - gap), widest


def detail_groups(sections) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """(tile title, [(label, value, warn)]) from tray_app's titled groups of
    "标题：内容" lines; a line without a title is shown as a bare value."""
    groups = []
    for section in sections:
        title, lines = section if isinstance(section, tuple) else ("", section)
        rows = []
        for line in lines:
            line = str(line).strip()
            if not line:
                continue
            label, _, value = line.partition("：")
            if not value:
                label, value = "", label
            rows.append((label, value, label == "读取状态" or value.startswith("⚠")))
        if rows:
            groups.append((title, rows))
    return groups


def step_text(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分钟" if seconds < 3600 else f"{seconds / 3600:g} 小时"


def letter_of(provider: str) -> str:
    import providers
    spec = providers.BY_KEY.get(provider)
    return spec.letter if spec else "?"


def in_tray_order(keys) -> list[str]:
    import providers
    return providers.tray_positions(keys)


def card_rows(model, snapshot, now: float) -> tuple:
    """The panel's header columns as card rows; the window name joins a
    countdown title so the card still says which window it is."""
    quota = getattr(model, "quota", None)
    if not quota:
        return view.quota_rows(snapshot, now)
    names = []
    if snapshot is not None and not (snapshot.pools and len(snapshot.pools) > 1):
        names = [view.window_name(snapshot.window_minutes), view.window_name(snapshot.secondary_window_minutes)]
    rows = []
    for slot, item in enumerate(quota[:3]):
        title, value = item[0], item[1]
        caption = item[2] if len(item) > 2 else ""
        tone = model.tone_of(slot) if hasattr(model, "tone_of") else ""
        figure = model.figure_of(slot) if hasattr(model, "figure_of") else None
        if not caption and slot < len(names) and title != names[slot]:
            caption = names[slot]
        rows.append((title, value, caption, tone, "".join(figure) if figure else None))
    return tuple(rows)


def build_cards(app, now: float | None = None) -> list[tuple[view.SourceCard, object]]:
    """(card, current tray icon image) for every shown source, in the tray's order."""
    now = now or time.time()
    result = []
    performance = (getattr(app, "performance", None) or {}).get("providers", {})
    scheduler = getattr(app, "scheduler", None)
    for provider in in_tray_order(app.snapshots):
        snapshot = app.snapshots[provider]
        data = performance.get(provider, {})
        try:
            model = app.panel(provider)
        except Exception:
            model = None
        if model is not None:
            status, tone, forecast, forecast_tone = model.status, model.status_tone, model.forecast, model.forecast_tone
        else:
            status, tone, forecast, forecast_tone = ("读取异常" if snapshot is not None and snapshot.error else "空闲"), "idle", "", "unknown"
        refresh = ""
        if scheduler is not None and hasattr(scheduler, "plan"):
            refresh = view.refresh_text(*scheduler.plan(provider, now))
        card = view.SourceCard(provider, letter_of(provider), PROVIDER_NAMES.get(provider, provider), status, tone,
                               card_rows(model, snapshot, now),
                               None if provider == "gemini" else data.get("display_tps"), float(data.get("output_tps_60") or 0.0),
                               int(data.get("active_sessions") or 0), forecast, forecast_tone,
                               (snapshot.error or "") if snapshot is not None else "", refresh)
        icon = getattr((getattr(app, "icons", None) or {}).get(provider), "icon", None)
        result.append((card, icon))
    return result


def interval_text(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds} 秒" if seconds < 60 else (f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds / 60:.1f} 分钟")


class FlowGrid:
    """Fixed-width tiles that wrap as the page narrows or widens, like the
    cards in Windows Settings: tiles never stretch; when a row takes one
    more or one fewer, every tile glides from where it was to its new
    place and the block's height eases to fit.

    A tile is (key, widget, fit, width): width is unscaled pixels, and
    fit(keys in its row) draws the tile and returns its height in pixels,
    or fit is None and the widget keeps its own height (a panel of text)."""

    def __init__(self, window, parent, gap=14, changed=None):
        self.window, self.gap, self.changed = window, gap, changed
        self.frame = window.tk.Frame(parent, bg=parent["bg"], height=1)
        self.frame.pack(fill="x")
        self.tiles = []
        self.spots = {}               # key -> (x, y) where the tile is drawn now, in pixels
        self.goals = {}               # key -> (x, y) a running glide is heading for
        self.heights = {}             # key -> last height of a tile sized by its own content
        self.rows = None
        self.widest = 0.0             # unscaled width of the longest row
        self.height = self.height_goal = 0
        self.width = 0
        self.pending = None
        self.frame.bind("<Configure>", self._resized, add="+")

    def _resized(self, event):
        if event.width != self.width:
            self.width = event.width
            self.layout()

    def set(self, tiles):
        keys = {tile[0] for tile in tiles}
        for key in list(self.spots):
            if key not in keys:
                self.spots.pop(key)
                self.goals.pop(key, None)
                self.window.motion.cancel(("tile", id(self), key))
        for key, widget, fit, _width in tiles:
            if fit is None and not getattr(widget, "flow_watched", False):
                widget.flow_watched = True     # its height follows its wrapped text
                widget.bind("<Configure>", lambda event, key=key: self._grown(key, event.height), add="+")
        self.tiles = list(tiles)

    def _grown(self, key, height):
        """Re-flow when a self-sized tile's height changes (not when it moves)."""
        if self.heights.get(key) != height:
            self.heights[key] = height
            self.soon()

    def soon(self):
        if self.pending is None:
            self.pending = self.frame.after_idle(self._settle)

    def _settle(self):
        self.pending = None
        self.layout()

    def layout(self):
        motion, scale = self.window.motion, self.window.scale
        width = self.frame.winfo_width()
        if width <= 1:
            return
        if not self.tiles:
            self._height(1, False)
            return

        def heights_for(rows):
            heights = [0.0] * len(self.tiles)
            for row in rows:
                keys = [self.tiles[index][0] for index in row]
                for index in row:
                    _key, widget, fit, _width = self.tiles[index]
                    drawn = fit(keys) if fit else None
                    heights[index] = (drawn if drawn else widget.winfo_reqheight()) / scale
            return heights

        rows, spots, total, widest = flow_spots(width / scale, [tile[3] for tile in self.tiles], heights_for, self.gap)
        shape = tuple(tuple(self.tiles[index][0] for index in row) for row in rows)
        wrapped = self.rows is not None and shape != self.rows
        self.rows = shape
        for (key, widget, _fit, _width), (left, top, size) in zip(self.tiles, spots):
            goal = (round(left * scale), round(top * scale))
            tile_width = round(size * scale)
            name = ("tile", id(self), key)
            if key in self.spots and (wrapped or motion.busy(name)) and motion.enabled:
                widget.place_configure(width=tile_width)
                if motion.busy(name) and self.goals.get(key) == goal:
                    continue                  # already gliding there; restarting would never arrive

                def put(value, widget=widget, key=key):
                    self.spots[key] = value
                    widget.place_configure(x=round(value[0]), y=round(value[1]))
                self.goals[key] = goal
                motion.to(name, self.spots[key], goal, .38, put)
            else:
                motion.cancel(name)
                self.spots[key] = self.goals[key] = goal
                widget.place(x=goal[0], y=goal[1], width=tile_width)
        self._height(round(total * scale), wrapped)
        if widest != self.widest:
            self.widest = widest
            if self.changed:
                self.changed()

    def _height(self, height, glide):
        name = ("flow-height", id(self))
        motion = self.window.motion
        if height == (self.height_goal if motion.busy(name) else self.height):
            return
        self.height_goal = height

        def put(value):
            self.height = round(value)
            self.frame.configure(height=max(1, self.height))
        if (glide or motion.busy(name)) and motion.enabled and self.height > 1:
            motion.to(name, self.height, height, .38, put)
        else:
            motion.cancel(name)
            put(height)


class SettingsController:
    """Keep Tk on one UI thread; tray callbacks only enqueue commands."""
    def __init__(self, app):
        self.app = app
        self.commands = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()

    def show(self, page=None):
        with self.lock:
            self.commands.put(("show", page))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="settings-ui", daemon=True)
                self.thread.start()

    def close(self):
        self.commands.put(("close",))

    def _run(self):
        SettingsWindow(self.app, self.commands).run()


class SettingsWindow:
    """Dark, panel-styled settings: a sidebar, five pages, a detail page per
    source and a save bar. Only the page on screen is laid out, so a resize
    reflows one page; motion (page slides, switches, hovers, scrolling) runs
    through ui_motion on the Tk loop."""

    CHART_H = 172
    CHART_MIN_W = 620

    def __init__(self, app, commands):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import messagebox
        from PIL import ImageTk
        self.tk, self.tkfont, self.messagebox, self.ImageTk = tk, tkfont, messagebox, ImageTk
        self.app, self.commands = app, commands
        root = self.root = tk.Tk()
        root.withdraw()
        root.title("环流 · Circlow 设置")
        self._window_icon(root)
        root.configure(bg=view.BG)
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        S = self.S = lambda value: round(value * self.scale)  # noqa: E731
        # Pictures are drawn `f` times larger than their Tk size. Tk shows one
        # image pixel per point, so on a 2x (Retina) Mac screen its copy is
        # soft; a frontend can lay the sharp original over it (on_picture).
        self.f = picture_factor()
        self.hires = {}               # PhotoImage name -> the picture at f
        self.on_picture = None        # callback(widget) after a label's picture changes
        root.geometry(f"{S(1060)}x{S(760)}")
        root.minsize(S(820), S(600))
        root.protocol("WM_DELETE_WINDOW", self.close)
        families = set(tkfont.families(root))
        self.family = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Noto Sans CJK SC", "Segoe UI",
                                              "PingFang SC")
                            if name in families), "TkDefaultFont")
        self.motion = ui_motion.Motion(root)
        root.update_idletasks()
        ui_motion.keep_unchanged_widgets(root)
        ui_motion.dark_title_bar(root, view.SIDEBAR, view.INK, view.BORDER)
        self.measures = {}            # font -> cached character widths
        self.images = {}              # keep PhotoImage references alive
        self.art_cache = {}           # rounded chrome pictures by look
        self.page = None
        self.vars = {}
        self.origins = {}
        self.notice = ("", 0.0)
        self.iq_selected: list[str] = []
        self.cards = []               # (SourceCard, icon) from the last live read
        self.card_art = {}            # provider -> (signature, normal, hovered)
        self.card_lit = {}            # provider -> 0..1 hover glow
        self.grid_shape = None
        self.detail_provider = None
        self.charts = {}
        self.range_controls = []
        self.chart_minutes = self._saved_minutes()
        self.zoom_dismissed = None    # start of the fast run the user zoomed back out of
        self.reflow = ui_motion.Throttle(root, 30, self._reflow)
        self._layout()

    def _window_icon(self, root) -> None:
        """The app's icon on the window and its taskbar button (Windows), or
        on the window from source on macOS (a build shows the bundle's)."""
        try:
            ico = app_identity.icon_file(".ico") if sys.platform == "win32" else None
            if ico is not None:
                root.iconbitmap(default=str(ico))
                return
            png = app_identity.icon_file(".png")
            if png is not None:
                from PIL import Image
                with Image.open(png) as picture:
                    self._icon_photo = self.ImageTk.PhotoImage(picture.convert("RGBA").resize((256, 256), Image.LANCZOS))
                root.iconphoto(True, self._icon_photo)
        except (OSError, self.tk.TclError):
            pass

    def _saved_minutes(self) -> int:
        try:
            value = int(getattr(self.app, "preference", lambda _name, default=None: default)("chart_minutes", 30))
        except (TypeError, ValueError):
            value = 30
        return value if value in view.CHART_RANGES else 30

    # ----- small widgets -------------------------------------------------------------
    def font(self, size=10, weight="normal"):
        return (self.family, size, weight)

    def measure_for(self, font) -> ui_motion.CharWidths:
        key = str(font)
        if key not in self.measures:
            self.measures[key] = ui_motion.CharWidths(self.tkfont.Font(root=self.root, font=font))
        return self.measures[key]

    def label(self, parent, text="", size=10, color=view.INK, weight="normal", **kw):
        return self.tk.Label(parent, text=text, bg=kw.pop("bg", parent["bg"]), fg=color, font=self.font(size, weight),
                             justify="left", anchor="w", **kw)

    def para(self, parent, text="", size=9, color=view.INK_3, **kw):
        """A paragraph that wraps to the width it is given (pack it with fill="x").
        Change its text with set_text so it is wrapped again."""
        widget = self.label(parent, text, size, color, **kw)
        widget.raw, widget.wrapped_at = text, None
        widget.measure = self.measure_for(widget.cget("font"))

        def wrap(event):
            width = max(self.S(160), event.width - self.S(8))
            if widget.wrapped_at != (width, widget.raw):
                widget.wrapped_at = (width, widget.raw)
                widget.configure(text=wrap_text(widget.raw, widget.measure, width))
        widget.bind("<Configure>", wrap, add="+")
        return widget

    @staticmethod
    def set_text(widget, text: str, **kw) -> None:
        """New text for a paragraph (wrapped to its current width) or a plain label."""
        if hasattr(widget, "raw"):
            if widget.raw != text:
                widget.raw = text
                width = (widget.wrapped_at or (0,))[0] or widget.winfo_width()
                widget.wrapped_at = (width, text)
                kw["text"] = wrap_text(text, widget.measure, width) if width > 1 else text
        elif widget.cget("text") != text:
            kw["text"] = text
        for name in [name for name in kw if name != "text"]:
            if widget.cget(name) == kw[name]:
                kw.pop(name)
        if kw:
            widget.configure(**kw)

    def fade(self, widget, duration=.14, **colors):
        """Tween a widget's colour options (bg=, fg=) to new hex values."""
        start = {name: str(widget.cget(name)) for name in colors}
        if all(start[name] == colors[name] for name in colors):
            self.motion.cancel(("fade", str(widget)))
            return

        def step(fraction):
            if widget.winfo_exists():
                widget.configure(**{name: ui_motion.blend(start[name], goal, fraction) for name, goal in colors.items()})
        self.motion.run(("fade", str(widget)), duration, step)

    # Rounded chrome. Tk draws only square frames, so every rounded surface is
    # a Pillow picture (drawn at 3x and scaled down, so the curve is smooth) or
    # a square frame with four such corner pictures laid over its corners.
    def rounded(self, size, radius, fill, outside, edge=None):
        """A rounded block of Tk size `size`, drawn at f times that."""
        from PIL import Image, ImageDraw
        key = ("rounded", size, radius, fill, outside, edge, self.f)
        art = self.art_cache.get(key)
        if art is None:
            k, f = 3, self.f
            big = Image.new("RGB", (size[0] * f * k, size[1] * f * k), outside)
            extra = {"outline": edge, "width": max(1, round(self.scale)) * f * k} if edge else {}
            ImageDraw.Draw(big).rounded_rectangle((0, 0, size[0] * f * k - 1, size[1] * f * k - 1), radius=radius * f * k,
                                                  fill=fill, **extra)
            art = big.resize((size[0] * f, size[1] * f), Image.LANCZOS)
            if len(self.art_cache) > 600:
                self.art_cache.clear()
            self.art_cache[key] = art
        return art.copy()

    def show_art(self, widget, image, duration=.14):
        """Put a picture on a label, cross-fading from the one it shows."""
        from PIL import Image

        def put(picture):
            widget.art = picture
            widget.photo = self.photo(picture, getattr(widget, "photo", None))
            if str(widget.cget("image")) != str(widget.photo):
                widget.configure(image=widget.photo)
            self.picture_changed(widget)
        current = getattr(widget, "art", None)
        name = ("art", str(widget))
        if current is None or current.size != image.size or duration <= 0 or not self.motion.enabled:
            self.motion.cancel(name)
            put(image)
            return
        self.motion.run(name, duration, lambda fraction: put(Image.blend(current, image, fraction)))

    def photo(self, picture, photo=None):
        """The Tk photo for a picture drawn at f: Tk gets a copy at its own
        size (pasted into `photo` when that is the same size), the original
        is kept in self.hires for a sharp overlay."""
        from PIL import Image
        f = self.f
        small = picture if f == 1 else picture.resize((max(1, round(picture.width / f)), max(1, round(picture.height / f))),
                                                     Image.LANCZOS)
        if photo is not None and (photo.width(), photo.height()) == small.size:
            photo.paste(small)
        else:
            photo = self.ImageTk.PhotoImage(small)
        if f != 1:
            self.hires[str(photo)] = picture
        return photo

    def picture_changed(self, widget):
        if self.on_picture is not None:
            self.on_picture(widget)

    def text_art(self, size, radius, fill, outside, text, ink, points=10, weight="regular", left=None):
        """A rounded block with one line of text, centred or from `left` (Tk sizes; drawn at f)."""
        from PIL import ImageDraw
        from detail_card import ui_font
        f = self.f
        image = self.rounded(size, radius, fill, outside)
        face = ui_font(round(points * 4 / 3 * self.scale * f), weight)
        draw = ImageDraw.Draw(image)
        if left is None:
            draw.text((size[0] * f / 2, size[1] * f / 2), text, font=face, fill=ink, anchor="mm")
        else:
            draw.text((left * f, size[1] * f / 2), text, font=face, fill=ink, anchor="lm")
        return image

    def text_width(self, text, points=10, weight="regular") -> int:
        from PIL import Image, ImageDraw
        from detail_card import ui_font
        face = ui_font(round(points * 4 / 3 * self.scale), weight)
        return round(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(text, font=face))

    def corner_art(self, radius, outside, fill, edge):
        key = ("corners", radius, outside, fill, edge)
        if key not in self.images:
            whole = self.rounded((2 * radius, 2 * radius), radius, fill, outside, edge)
            r = radius * self.f
            self.images[key] = [self.photo(whole.crop(box))
                                for box in ((0, 0, r, r), (r, 0, 2 * r, r), (0, r, r, 2 * r), (r, r, 2 * r, 2 * r))]
        return self.images[key]

    def panel(self, parent, fill=view.SURFACE, edge=view.BORDER, padx=18, pady=14, radius=12):
        """A rounded surface: a hairline edge, the fill, and four corner
        pictures over the square corners. Returns the outer frame; put
        content in its .inner, recolour the edge with .paint(colour)."""
        tk, S = self.tk, self.S
        outside = str(parent["bg"])
        line = max(1, round(self.scale))
        outer = tk.Frame(parent, bg=edge)
        inner = tk.Frame(outer, bg=fill, padx=S(padx), pady=S(pady))
        inner.pack(fill="both", expand=True, padx=line, pady=line)
        corners = []
        for relx, rely, anchor in ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se")):
            corner = tk.Label(outer, bd=0, padx=0, pady=0, highlightthickness=0, bg=fill)
            corner.place(relx=relx, rely=rely, anchor=anchor)
            corners.append(corner)

        def paint(color):
            outer.configure(bg=color)
            for corner, image in zip(corners, self.corner_art(S(radius), outside, fill, color)):
                corner.configure(image=image)
                self.picture_changed(corner)
        paint(edge)
        outer.inner, outer.paint = inner, paint
        return outer

    def badge(self, parent, letter, color, size=22):
        widget = self.tk.Label(parent, bd=0, padx=0, pady=0, bg=parent["bg"])
        side = self.S(size)
        self.show_art(widget, self.text_art((side, side), self.S(6), color, str(parent["bg"]), letter, "#1f1e1c",
                                            points=size * .44, weight="bold"), 0)
        return widget

    def pill(self, parent, text, fill, ink):
        widget = self.tk.Label(parent, bd=0, padx=0, pady=0, bg=parent["bg"])
        size = (self.text_width(text, 8) + self.S(14), self.S(19))
        self.show_art(widget, self.text_art(size, size[1] // 2, fill, str(parent["bg"]), text, ink, points=8), 0)
        return widget

    def button(self, parent, text, command, kind="ghost"):
        """A rounded button drawn as a picture; hover and press cross-fade."""
        S = self.S
        colors = {"primary": (view.GREEN, "#93d6b7", "#1f1e1c"), "ghost": (view.SURFACE_HI, "#3d3c36", view.INK),
                  "danger": ("#3a2524", "#4a2b29", "#ef8a7f"), "off": (view.SURFACE, view.SURFACE, view.INK_3)}
        weight = "bold" if kind == "primary" else "regular"
        size = (self.text_width(text, 10, weight) + S(30), S(32))
        outside = str(parent["bg"])
        widget = self.tk.Label(parent, bd=0, padx=0, pady=0, bg=outside, cursor="hand2")
        widget.kind, widget.enabled, widget.inside = kind, True, False

        def paint(pressed=False, now=False):
            base, lit, ink = colors[widget.kind if widget.enabled else "off"]
            fill = lit if widget.inside and widget.enabled else base
            if pressed:
                fill = ui_motion.blend(base, "#000000", .18)
            art = self.text_art(size, S(7), fill, outside, text, ink, 10, weight)
            self.show_art(widget, art, 0 if now else (.06 if pressed else .14))
            widget.configure(cursor="hand2" if widget.enabled else "arrow")

        def enter(_):
            widget.inside = True
            paint()

        def leave(_):
            widget.inside = False
            paint()

        def release(event):
            inside = 0 <= event.x < widget.winfo_width() and 0 <= event.y < widget.winfo_height()
            paint()
            if widget.enabled and inside:
                command()

        def set_enabled(on):
            if widget.enabled != on:
                widget.enabled = on
                paint()

        widget.set_enabled = set_enabled
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
        widget.bind("<ButtonPress-1>", lambda _: widget.enabled and paint(pressed=True))
        widget.bind("<ButtonRelease-1>", release)
        paint(now=True)
        return widget

    def link(self, parent, text, command, color=view.GREEN, size=9):
        widget = self.label(parent, text, size, color, cursor="hand2")
        lit = ui_motion.blend(color, "#ffffff", .25)
        widget.bind("<Button-1>", lambda _: command())
        widget.bind("<Enter>", lambda _: self.fade(widget, .12, fg=lit))
        widget.bind("<Leave>", lambda _: self.fade(widget, .12, fg=color))
        return widget

    def switch(self, parent, variable, command=None):
        tk, S = self.tk, self.S
        width, r = S(40), S(11)
        canvas = tk.Canvas(parent, width=width, height=2 * r, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        track = canvas.create_polygon(round_points(0, 0, width, 2 * r, r), smooth=True, splinesteps=12, outline="")
        knob = canvas.create_oval(0, 0, 0, 0, fill="#f6f4ee", outline="")
        state = {"at": 1.0 if variable.get() else 0.0}

        def place(at):
            state["at"] = at
            fill = ui_motion.blend("#4a4842", view.GREEN, at)
            canvas.itemconfigure(track, fill=fill)
            x = r + (width - 2 * r) * at
            canvas.coords(knob, x - S(8), r - S(8), x + S(8), r + S(8))

        def changed(*_):
            goal = 1.0 if variable.get() else 0.0
            if canvas.winfo_ismapped():
                self.motion.to(("switch", str(canvas)), state["at"], goal, .18, place)
            else:
                self.motion.cancel(("switch", str(canvas)))
                place(goal)

        def toggle(_):
            variable.set(not variable.get())
            if command:
                command()

        canvas.bind("<Button-1>", toggle)
        variable.trace_add("write", changed)
        place(state["at"])
        return canvas

    def segmented(self, parent, options, current, command):
        """A pill row of choices; the highlight slides to the one picked."""
        tk, S = self.tk, self.S
        face = self.font(9)
        measure = self.measure_for(face)
        widths = [measure(text) + S(24) for _value, text in options]
        height, inset = S(28), S(3)
        lefts, x = [], inset
        for width in widths:
            lefts.append(x)
            x += width
        canvas = tk.Canvas(parent, width=x + inset, height=height, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        canvas.create_polygon(round_points(1, 1, x + inset - 1, height - 1, height / 2 - 1), smooth=True, splinesteps=12,
                              fill=view.FIELD, outline=view.BORDER)
        pill = canvas.create_polygon(0, 0, 0, 0, smooth=True, splinesteps=12, fill=view.SURFACE_HI, outline="")
        texts = [canvas.create_text(left + width / 2, height / 2, text=text, font=face, fill=view.INK_3)
                 for left, width, (_value, text) in zip(lefts, widths, options)]
        values = [value for value, _text in options]
        state = {"index": values.index(current) if current in values else 0, "at": None, "hover": None}

        def place(span):
            state["at"] = span
            canvas.coords(pill, *round_points(span[0], inset, span[0] + span[1], height - inset, (height - 2 * inset) / 2))

        def ink():
            for index, item in enumerate(texts):
                color = view.INK if index == state["index"] else (view.INK_2 if index == state["hover"] else view.INK_3)
                canvas.itemconfigure(item, fill=color)

        def select(index, animate=True):
            state["index"] = index
            goal = (lefts[index], widths[index])
            if animate and state["at"] is not None:
                self.motion.to(("segment", str(canvas)), state["at"], goal, .24, place)
            else:
                place(goal)
            ink()

        def at(x):
            return next((index for index, left in enumerate(lefts) if left <= x < left + widths[index]), None)

        def click(event):
            index = at(event.x)
            if index is None:
                return
            if index != state["index"]:
                select(index)
            command(values[index])          # the picked one again: the chart's way back from auto-zoom

        def motion(event):
            if at(event.x) != state["hover"]:
                state["hover"] = at(event.x)
                ink()

        def leave(_):
            state["hover"] = None
            ink()

        canvas.bind("<Button-1>", click)
        canvas.bind("<Motion>", motion)
        canvas.bind("<Leave>", leave)
        canvas.select = lambda value, animate=True: value in values and select(values.index(value), animate)
        select(state["index"], animate=False)
        return canvas

    def card(self, parent, pady=(0, 12)):
        outer = self.panel(parent)
        outer.pack(fill="x", pady=(self.S(pady[0]), self.S(pady[1])))
        return outer.inner

    def setting(self, parent, title, detail, variable, command=None, badge=None):
        """A switch line: badge, title and explanation on the left, switch on the right."""
        line = self.tk.Frame(parent, bg=parent["bg"])
        line.pack(fill="x")
        if badge:
            letter, color = badge
            self.badge(line, letter, color).pack(side="left", anchor="n", padx=(0, self.S(12)), pady=(self.S(2), 0))
        self.switch(line, variable, command).pack(side="right", anchor="n", padx=(self.S(12), 0), pady=(self.S(2), 0))
        text = self.tk.Frame(line, bg=parent["bg"])
        text.pack(side="left", fill="x", expand=True)
        self.label(text, title, 11, weight="bold").pack(anchor="w")
        line.detail = self.para(text, detail)
        line.detail.pack(anchor="w", fill="x", pady=(self.S(2), 0))
        line.text = text
        return line

    def heading(self, parent, title, subtitle="", action=None):
        """The page's title row: a large title, one quiet line under it, and
        the page's action at the right end of the title's baseline."""
        top = self.tk.Frame(parent, bg=view.BG)
        top.pack(fill="x")
        if action is not None:
            self.button(top, *action).pack(side="right", anchor="n")
        self.label(top, title, 18, weight="bold").pack(side="left", anchor="w")
        under = self.tk.Frame(parent, bg=view.BG)
        under.pack(fill="x", pady=(self.S(3), self.S(20)))
        if subtitle:
            self.para(under, subtitle, 10, view.INK_2).pack(anchor="w", fill="x")
        return top

    def section(self, parent, title, note=""):
        """A group title between cards: bold, with its note trailing in grey;
        more air above it than below, so it belongs to what follows."""
        line = self.tk.Frame(parent, bg=view.BG)
        line.pack(fill="x", pady=(self.S(14), self.S(8)))
        self.label(line, title, 11, weight="bold").pack(side="left")
        if note:
            self.label(line, note, 9, view.INK_3).pack(side="left", padx=self.S(10), pady=(self.S(2), 0))
        return line

    def entry(self, parent, variable, secret=False):
        """A rounded text field whose edge turns green while it has focus."""
        box = self.panel(parent, view.FIELD, view.BORDER, padx=10, pady=6, radius=7)
        field = self.tk.Entry(box.inner, textvariable=variable, show="●" if secret else "", bg=view.FIELD, fg=view.INK,
                              insertbackground=view.INK, relief="flat", bd=0, font=self.font(10), highlightthickness=0)
        field.pack(fill="x")
        field.bind("<FocusIn>", lambda _: box.paint(view.GREEN), add="+")
        field.bind("<FocusOut>", lambda _: box.paint(view.BORDER), add="+")
        box.field = field
        return box

    def form(self, parent, rows):
        """(variable, title, hint, secret) rows: label, field, hint."""
        grid = self.tk.Frame(parent, bg=parent["bg"])
        grid.pack(fill="x", pady=(self.S(8), 0))
        grid.columnconfigure(1, weight=1)
        for index, (variable, title, hint, secret) in enumerate(rows):
            self.label(grid, title, 10, view.INK_2).grid(row=index, column=0, sticky="w", pady=self.S(5), padx=(0, self.S(16)))
            self.entry(grid, variable, secret).grid(row=index, column=1, sticky="ew", pady=self.S(4))
            self.label(grid, hint, 9, view.INK_3).grid(row=index, column=2, sticky="w", padx=(self.S(12), 0))
        return grid

    def picture(self, parent, key, image, **kw):
        """A label showing a Pillow image; a same-sized update is pasted into
        the existing photo instead of making a new one."""
        photo = self.images.get(key)
        widget = self.images.get(key + ":widget")
        if image.mode == "RGBA":
            # Tk blends a photo with an alpha channel in software on every
            # repaint; flattened onto the page colour it is a plain blit.
            from PIL import Image
            ground = Image.new("RGB", image.size, str(parent["bg"]))
            ground.paste(image, mask=image.getchannel("A"))
            image = ground
        new = self.photo(image, photo)
        if new is not photo:
            photo = self.images[key] = new
            if widget is not None and widget.winfo_exists():
                widget.configure(image=photo)
        if widget is None or not widget.winfo_exists():
            widget = self.tk.Label(parent, image=photo, bg=parent["bg"], bd=0, **kw)
            self.images[key + ":widget"] = widget
        self.picture_changed(widget)
        return widget

    def scrollable(self, page, key):
        """A page body that scrolls smoothly with the wheel when it outgrows the
        window, with a slim scroll thumb that can be dragged."""
        tk, S = self.tk, self.S
        canvas = tk.Canvas(page, bg=view.BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=view.BG)
        window = canvas.create_window(0, 0, window=inner, anchor="nw")
        bar = tk.Canvas(page, width=S(8), bg=view.BG, highlightthickness=0, bd=0)
        thumb = bar.create_line(0, 0, 0, 0, width=S(4), capstyle="round", fill=view.BORDER)
        state = {"goal": 0.0, "grab": None}

        def overflow():
            return max(0, inner.winfo_reqheight() - canvas.winfo_height())

        def region(_=None):
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), max(inner.winfo_reqheight(), canvas.winfo_height())))
            show_thumb()

        def show_thumb(*_):
            first, last = canvas.yview()
            if not overflow() or last - first >= .999:
                if bar.winfo_manager():
                    bar.place_forget()
                return
            if not bar.winfo_manager():
                bar.place(relx=1, x=-S(8), y=0, relheight=1, width=S(8))
            height = bar.winfo_height() or canvas.winfo_height()
            pad = S(4)
            bar.coords(thumb, S(4), pad + first * (height - 2 * pad), S(4), pad + last * (height - 2 * pad))

        def scroll_to(top):
            total = max(inner.winfo_reqheight(), canvas.winfo_height())
            canvas.yview_moveto(max(0.0, top) / total)

        def glide(goal, duration=.26):
            state["goal"] = max(0.0, min(float(overflow()), goal))
            self.motion.to(("scroll", key), canvas.canvasy(0), state["goal"], duration, scroll_to)

        def wheel(event):
            if self.page != key or not overflow():
                return
            notches = event.delta / 120 if event.delta else (1 if getattr(event, "num", 0) == 4 else -1)
            base = state["goal"] if self.motion.busy(("scroll", key)) else canvas.canvasy(0)
            glide(base - notches * S(96))

        def grab(event):
            state["grab"] = (event.y, canvas.canvasy(0))
            bar.itemconfigure(thumb, fill=view.INK_3)

        def drag(event):
            if state["grab"] is None:
                return
            start_y, start_top = state["grab"]
            total = max(inner.winfo_reqheight(), 1)
            self.motion.cancel(("scroll", key))
            top = max(0.0, min(float(overflow()), start_top + (event.y - start_y) * total / max(1, bar.winfo_height())))
            state["goal"] = top
            scroll_to(top)

        def drop(_):
            state["grab"] = None
            bar.itemconfigure(thumb, fill=view.BORDER)

        def apply_width():
            width = max(1, canvas.winfo_width() - S(14))
            if state.get("width") != width:
                state["width"] = inner.content_width = width
                canvas.itemconfigure(window, width=width)
                if self.page == key:
                    self.reflow()
            region()

        # Every widget is a native child window on Windows, so re-laying a page
        # costs far more than resizing the window: while an edge is dragged the
        # content follows every other frame and the frame itself at full rate.
        follow = ui_motion.Throttle(self.root, 24, apply_width)

        def fit(_event):
            if state.get("width") is None:
                apply_width()
            else:
                follow()

        inner.bind("<Configure>", region)
        canvas.bind("<Configure>", fit)
        canvas.configure(yscrollcommand=show_thumb)
        bar.bind("<ButtonPress-1>", grab)
        bar.bind("<B1-Motion>", drag)
        bar.bind("<ButtonRelease-1>", drop)
        bar.bind("<Enter>", lambda _: state["grab"] is None and bar.itemconfigure(thumb, fill=view.INK_3))
        bar.bind("<Leave>", lambda _: state["grab"] is None and bar.itemconfigure(thumb, fill=view.BORDER))
        canvas.bind_all("<MouseWheel>", wheel, add="+")
        canvas.bind_all("<Button-4>", wheel, add="+")
        canvas.bind_all("<Button-5>", wheel, add="+")
        inner.canvas = canvas
        inner.glide = glide

        def home():
            self.motion.cancel(("scroll", key))
            state["goal"] = 0.0
            canvas.yview_moveto(0)
        inner.home = home
        return inner

    # ----- layout ----------------------------------------------------------------------
    def _layout(self):
        tk, S = self.tk, self.S
        side = self.side = tk.Frame(self.root, bg=view.SIDEBAR, width=S(212))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=view.SIDEBAR)
        brand.pack(fill="x", padx=S(16), pady=(S(22), S(24)))
        dots = tk.Canvas(brand, width=S(26), height=S(26), bg=view.SIDEBAR, highlightthickness=0)
        for index, provider in enumerate(("claude", "codex", "gemini")):
            color = view.hex_color(view.accent(letter_of(provider)))
            x = S(1 + index * 7)
            dots.create_oval(x, S(8), x + S(11), S(19), fill=color, outline=view.SIDEBAR, width=S(2))
        dots.pack(side="left", anchor="n")
        names = tk.Frame(brand, bg=view.SIDEBAR)
        names.pack(side="left", padx=(S(6), 0))
        self.label(names, "环流 · Circlow", 11, weight="bold").pack(anchor="w")
        self.label(names, "AI 额度与 TPS 流速", 9, view.INK_3).pack(anchor="w")
        self.nav = {}
        self.nav_hover = None
        for key, title in PAGES:
            item = tk.Label(side, bd=0, padx=0, pady=0, bg=view.SIDEBAR, cursor="hand2", anchor="w")
            item.title = title
            item.pack(anchor="w", padx=S(10), pady=S(2))
            item.bind("<Button-1>", lambda _, key=key: self.show_page(key))
            item.bind("<Enter>", lambda _, key=key: self._nav_hover(key))
            item.bind("<Leave>", lambda _: self._nav_hover(None))
            self.nav[key] = item
        self._paint_nav(now=True)
        self.nav_bar = tk.Frame(side, bg=view.GREEN, width=S(3))
        self.nav_at = None
        side.bind("<Configure>", lambda _: self._place_nav(animate=False), add="+")
        stamp = getattr(self.app, "build_stamp", lambda: "")()
        foot = tk.Frame(side, bg=view.SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=S(20), pady=S(18))
        if stamp:
            self.label(foot, f"构建 {stamp}", 8, view.INK_3).pack(anchor="w")
        self.label(foot, "托盘右键或面板齿轮可打开此页", 8, view.INK_3, wraplength=S(176)).pack(anchor="w")

        main = tk.Frame(self.root, bg=view.BG)
        main.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(main, bg=view.SIDEBAR)
        bar.pack(side="bottom", fill="x")
        footer = tk.Frame(bar, bg=view.SIDEBAR)
        footer.pack(fill="x", padx=S(28), pady=S(12))
        self.status = tk.StringVar()
        self.status_label = self.label(footer, "", 10, view.INK_2, textvariable=self.status)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.button(footer, "关闭", self.close).pack(side="right")
        self.body = tk.Frame(main, bg=view.BG)
        self.body.pack(fill="both", expand=True, padx=(S(28), S(14)), pady=(S(22), S(6)))
        keys = [key for key, _ in PAGES] + ["detail"]
        self.pages = {key: tk.Frame(self.body, bg=view.BG) for key in keys}
        self.bodies = {key: self.scrollable(frame, key) for key, frame in self.pages.items()}
        self._overview(self.bodies["overview"])
        self._detail(self.bodies["detail"])
        self._sources(self.bodies["sources"])
        self._iq(self.bodies["iq"])
        self._tasks(self.bodies["tasks"])
        self._general(self.bodies["general"])
        self.root.bind("<Escape>", lambda _: self.page == "detail" and self.show_page("overview", slide="back"))
        self.root.bind("<Control-s>", lambda _: self.save())
        self.show_page("overview", slide=None)

    def _nav_key(self):
        return "overview" if self.page == "detail" else self.page

    def _nav_hover(self, key):
        self.nav_hover = key
        self._paint_nav()

    def _paint_nav(self, now=False):
        S = self.S
        current = self._nav_key()
        hover_bg = ui_motion.blend(view.SIDEBAR, view.SURFACE, .5)
        for name, item in self.nav.items():
            bg = view.SURFACE if name == current else (hover_bg if name == self.nav_hover else view.SIDEBAR)
            fg = view.INK if name == current else view.INK_2
            art = self.text_art((S(192), S(38)), S(7), bg, view.SIDEBAR, item.title, fg, 11, left=S(18))
            self.show_art(item, art, 0 if now else .16)

    def _place_nav(self, animate=True):
        item = self.nav.get(self._nav_key())
        if item is None or not item.winfo_ismapped():
            return
        goal = (item.winfo_y() + (item.winfo_height() - self.S(16)) / 2, self.S(16))
        if goal[1] <= 0:
            return

        def place(value):
            self.nav_at = value
            self.nav_bar.place(x=self.S(13), y=round(value[0]), width=self.S(3), height=round(value[1]))
        if animate and self.nav_at is not None:
            self.motion.to("nav", self.nav_at, goal, .26, place)
        else:
            self.motion.cancel("nav")
            place(goal)
        self.nav_bar.lift()

    def show_page(self, key, slide="rise"):
        """Bring a page in: it rises into place from the sidebar, or slides
        sideways into and out of a source's detail page."""
        key = PAGE_ALIASES.get(key, key)
        if key not in self.pages:
            return
        if key == self.page:
            self.refresh_live()
            return
        old = self.pages.get(self.page)
        self.page = key
        self._paint_nav(now=slide is None)
        self._place_nav(animate=slide is not None)
        frame = self.pages[key]
        offset = {"forward": (self.S(40), 0), "back": (-self.S(40), 0), "rise": (0, self.S(16))}.get(slide, (0, 0))
        self.motion.cancel("page")
        frame.place(relx=0, rely=0, relwidth=1, relheight=1, x=offset[0], y=offset[1])
        frame.lift()
        if old is not None and old is not frame:
            old.place_forget()
        self.bodies[key].home()
        self.refresh_live()
        if offset != (0, 0):
            self.motion.to("page", offset, (0, 0), .28,
                           lambda value: frame.place_configure(x=round(value[0]), y=round(value[1])))

    def _reflow(self):
        """Width-dependent pictures for the page on screen, after a resize."""
        if self.root.state() == "withdrawn":
            return
        try:
            if self.page == "overview":
                self._layout_overview()
            elif self.page == "detail":
                self._layout_detail()
            elif self.page == "tasks":
                self._fill_tasks()
        except Exception:
            logging.getLogger(__name__).exception("settings reflow failed")

    def page_width(self, key) -> float:
        """Usable width of a page in unscaled pixels."""
        width = getattr(self.bodies[key], "content_width", 0) or self.bodies[key].winfo_width()
        if width <= 1:
            width = self.body.winfo_width() - self.S(14)
        return max(view.CARD_W, (width if width > 1 else self.S(780)) / self.scale)

    # ----- output chart (概览 and source details) -------------------------------------------
    def _activity_panel(self, parent, name, providers):
        tk, S = self.tk, self.S
        box = tk.Frame(parent, bg=view.BG)          # as wide as the chart, so the range picker ends with it
        box.pack(anchor="w")
        head = tk.Frame(box, bg=view.BG)
        head.pack(fill="x", pady=(S(8), S(8)))
        title = self.label(head, "", 11, weight="bold")
        title.pack(side="left")
        note = self.label(head, "", 9, view.INK_3)
        note.pack(side="left", padx=(S(10), 0))
        back = self.link(head, "", lambda: self.leave_zoom())
        control = self.segmented(head, [(minutes, view.RANGE_NAMES[minutes]) for minutes in view.CHART_RANGES],
                                 self.chart_minutes, self.set_chart_minutes)
        control.pack(side="right")
        self.range_controls.append(control)
        holder = tk.Frame(box, bg=view.BG)
        holder.pack(anchor="w")
        chart = {"name": name, "title": title, "note": note, "back": back, "holder": holder, "providers": providers,
                 "activity": None, "hover": None, "grow": 1.0, "width": None, "drawn": None, "zoom": None, "burst": None}
        self.charts[name] = chart
        return chart

    def set_chart_minutes(self, minutes):
        if minutes == self.chart_minutes:
            self.leave_zoom()
            return
        self._dismiss_burst()           # a range picked during a fast run is the view wanted for it
        self.chart_minutes = minutes
        for control in self.range_controls:
            control.select(minutes, animate=control.winfo_ismapped())
        if hasattr(self.app, "save_preferences"):
            try:
                self.app.save_preferences({"chart_minutes": minutes})
            except OSError:
                logging.getLogger(__name__).exception("chart range not saved")
        for chart in self.charts.values():
            chart["activity"] = None
            chart["hover"] = None
        chart = self.charts.get("detail" if self.page == "detail" else "overview")
        if chart is not None:
            self._regrow(chart)

    def _regrow(self, chart):
        chart["grow"] = 0.0

        def grow(fraction):
            chart["grow"] = fraction
            self._paint_chart(chart)
        self.motion.run(("grow", chart["name"]), .5, grow, ease=lambda t: t)

    def _dismiss_burst(self):
        runs = [chart["burst"].start for chart in self.charts.values() if chart["zoom"] and chart["burst"]]
        if runs:
            self.zoom_dismissed = min(runs)

    def leave_zoom(self):
        """Back to the picked range for the rest of this fast run."""
        if not any(chart["zoom"] for chart in self.charts.values()):
            return
        self._dismiss_burst()
        for chart in self.charts.values():
            chart["activity"] = None
            chart["hover"] = None
        chart = self.charts.get("detail" if self.page == "detail" else "overview")
        if chart is not None:
            self._paint_chart(chart, fresh=True)

    def _chart_span(self, chart, events, now, providers) -> int:
        """The picked range, or a closer one while output runs fast, so the
        run fills the chart instead of being a sliver at its right end."""
        run = view.burst(events, now, providers)
        chart["burst"] = run
        if run is not None and self.zoom_dismissed is not None and abs(run.start - self.zoom_dismissed) <= 90:
            run = None                  # the user zoomed back out of this run
        chart["zoom"] = view.zoom_minutes(run, self.chart_minutes, now)
        return chart["zoom"] or self.chart_minutes

    def _events(self):
        try:
            return list(self.app.tps_monitor.events)
        except (AttributeError, RuntimeError):
            return []

    def _paint_chart(self, chart, fresh=False):
        """Bucket the events (on a live read or a range change) and draw the
        chart at the page's width, with the hovered bar picked out."""
        providers = chart["providers"]() if callable(chart["providers"]) else chart["providers"]
        if fresh or chart["activity"] is None or tuple(providers) != chart["activity"].providers:
            events, now, before = self._events(), time.time(), chart["activity"]
            minutes = self._chart_span(chart, events, now, providers)
            store = getattr(self.app, "usage", None)
            if minutes > 1440 and store is not None:
                # Replies are kept one by one for a day; longer ranges use the saved hourly totals.
                events = store.hour_rows(providers, now - minutes * 60, now)
            chart["activity"] = view.activity(events, now, minutes, providers)
            if before is not None and before.minutes != minutes and chart["grow"] >= 1 and chart["holder"].winfo_ismapped():
                self._regrow(chart)     # zooming in or out: the bars rise again at the new size
        activity = chart["activity"]
        zoom = chart["zoom"]
        flow = self.card_flow if chart["name"] == "overview" else self.detail_flow
        # Ends with the tiles above it, never narrower than a readable chart.
        width = min(self.page_width(chart["name"]), max(self.CHART_MIN_W, flow.widest))
        chart["width"] = width
        self.set_text(chart["title"], f"最近 {view.RANGE_NAMES[activity.minutes]}输出")
        if zoom:
            self.set_text(chart["note"], f"高速输出 {chart['burst'].rate:.0f} tok/s，已自动拉近 · 每根柱 {step_text(activity.step)}")
            self.set_text(chart["back"], f"看 {view.RANGE_NAMES[self.chart_minutes]}")
            if not chart["back"].winfo_manager():
                chart["back"].pack(side="left", padx=(self.S(8), 0))
        else:
            self.set_text(chart["note"], f"每根柱 {step_text(activity.step)} · 悬停查看数值")
            if chart["back"].winfo_manager():
                chart["back"].pack_forget()
        drawn = (activity, round(width), chart["hover"], round(chart["grow"], 3), bool(zoom))
        if chart["drawn"] == drawn:
            return
        chart["drawn"] = drawn
        image = view.activity_chart(activity, providers, self.scale * self.f, width=round(width), height=self.CHART_H,
                                    hover=chart["hover"], grow=chart["grow"], trend=bool(zoom))
        widget = self.picture(chart["holder"], "chart:" + chart["name"], image)
        if not widget.winfo_manager():
            widget.pack(anchor="w")
            widget.bind("<Motion>", lambda event: self._chart_hover(chart, event.x / self.scale))
            widget.bind("<Leave>", lambda _: self._chart_hover(chart, None))

    def _chart_hover(self, chart, x):
        activity = chart["activity"]
        index = None
        if activity is not None and x is not None and chart["width"]:
            index = view.chart_index(x, chart["width"], self.CHART_H, len(activity.buckets))
        if index != chart["hover"]:
            chart["hover"] = index
            self._paint_chart(chart)

    # ----- 概览 ---------------------------------------------------------------------------
    def _overview(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "概览", "与托盘和悬停面板同步的来源卡片，以及最近的输出。点击卡片查看详情。",
                     ("立即刷新", self.app.request_refresh))
        self.tray_notice = self.panel(page, "#3a3122", "#4a3d27", padx=16, pady=10)
        notice = self.tray_notice.inner
        self.button(notice, "打开任务栏设置", self.open_taskbar_settings).pack(side="right", padx=(S(12), 0))
        self.tray_notice_text = self.para(notice, "", 10, view.WARN, bg="#3a3122")
        self.tray_notice_text.pack(side="left", fill="x", expand=True)
        self.card_flow = FlowGrid(self, page, changed=self.reflow)
        self.card_grid = self.card_flow.frame
        self._activity_panel(page, "overview", lambda: {provider: letter_of(provider) for provider in in_tray_order(self.app.snapshots)})

    def _fill_overview(self):
        S = self.S
        hidden = int(getattr(self.app, "hidden_slots", 0) or 0)
        if hidden:
            text = f"有 {hidden} 个托盘图标收在 ^ 隐藏区，任务栏上会缺少排在最后的来源。请在任务栏设置中打开全部环流图标。"
            self.set_text(self.tray_notice_text, text)
            if not self.tray_notice.winfo_manager():
                self.tray_notice.pack(fill="x", pady=(0, S(12)), before=self.card_grid)
        elif self.tray_notice.winfo_manager():
            self.tray_notice.pack_forget()
        self.cards = build_cards(self.app)
        self._layout_overview()
        self._paint_chart(self.charts["overview"], fresh=True)

    def _layout_overview(self):
        """Lay the cards into the flowing grid from the last read; a resize
        reflows them without asking the tray for new figures."""
        cards = {card.provider: (card, icon) for card, icon in self.cards}
        tiles = []
        for provider, (card, icon) in cards.items():
            def fit(row, card=card, icon=icon):
                slots = max(len(cards[key][0].rows) or 1 for key in row if key in cards)
                return round(self._draw_card(card, icon, view.CARD_W, slots).size[1] / self.f)   # Tk size, not picture pixels
            if self.images.get("card:" + provider + ":widget") is None:
                self._draw_card(card, icon, view.CARD_W, len(card.rows) or 1)
            tiles.append((provider, self.images["card:" + provider + ":widget"], fit, view.CARD_W))
        self.card_flow.set(tiles)
        self.card_flow.layout()
        self._paint_chart(self.charts["overview"])

    def _draw_card(self, card, icon, width, slots):
        """The card picture at this width; the hover version is drawn only
        once the pointer comes onto the card."""
        signature = (card, id(icon), icon.tobytes() if icon is not None else None, width, slots)
        art = self.card_art.get(card.provider)
        if art is None or art[0] != signature:
            normal = view.source_card(card, icon, self.scale * self.f, hover=False, slots=slots, width=width)
            art = self.card_art[card.provider] = [signature, normal, None, icon]
            self._show_card(card.provider)
        return art[1]

    def _show_card(self, provider):
        art = self.card_art.get(provider)
        if art is None:
            return
        glow = self.card_lit.get(provider, 0.0)
        if glow > 0 and art[2] is None:
            card, _icon_id, _bytes, width, slots = art[0]
            art[2] = view.source_card(card, art[3], self.scale * self.f, hover=True, slots=slots, width=width)
        from PIL import Image
        image = art[1] if glow <= 0 else art[2] if glow >= 1 else Image.blend(art[1], art[2], glow)
        key = "card:" + provider
        fresh = self.images.get(key + ":widget") is None
        widget = self.picture(self.card_grid, key, image, cursor="hand2")
        if fresh:
            widget.bind("<ButtonRelease-1>", lambda _: self.show_detail(provider))
            widget.bind("<Enter>", lambda _: self._hover_card(provider, True))
            widget.bind("<Leave>", lambda _: self._hover_card(provider, False))

    def _hover_card(self, provider, on):
        start = self.card_lit.get(provider, 0.0)

        def step(value):
            self.card_lit[provider] = value
            self._show_card(provider)
        self.motion.to(("card", provider), start, 1.0 if on else 0.0, .16 if on else .24, step)

    def open_taskbar_settings(self):
        if hasattr(os, "startfile"):
            os.startfile("ms-settings:taskbar")  # type: ignore[attr-defined]  # the user's own settings page

    # ----- source details -------------------------------------------------------------------
    def _detail(self, page):
        tk, S = self.tk, self.S
        top = tk.Frame(page, bg=view.BG)
        top.pack(fill="x")
        self.link(top, "‹ 概览", lambda: self.show_page("overview", slide="back"), color=view.INK_2, size=11).pack(side="left")
        self.detail_title = self.label(top, "", 16, weight="bold")
        self.detail_title.pack(side="left", padx=(S(14), 0))
        self.button(top, "立即刷新", self.app.request_refresh).pack(side="right", anchor="n")
        tk.Frame(page, bg=view.BG, height=S(10)).pack(fill="x")
        # The source card and one tile per group of details, all the same size, flowing like the overview.
        self.detail_flow = FlowGrid(self, page, changed=self.reflow)
        self.detail_card = self.detail_flow.frame
        self.detail_groups = []
        self.tile_art = {}            # tile key -> signature of the picture it shows
        self._activity_panel(page, "detail", lambda: {self.detail_provider: letter_of(self.detail_provider)}
                             if self.detail_provider else {})

    def show_detail(self, provider):
        if provider not in getattr(self.app, "snapshots", {}):
            return
        changed = provider != self.detail_provider
        self.detail_provider = provider
        if changed:
            self.charts["detail"]["activity"] = None
            self.charts["detail"]["hover"] = None
        self.show_page("detail", slide="forward")

    def _fill_detail(self):
        provider = self.detail_provider
        if provider is None or provider not in self.app.snapshots:
            self.show_page("overview", slide="back")
            return
        self.cards = build_cards(self.app)
        self.set_text(self.detail_title, PROVIDER_NAMES.get(provider, provider))
        sections = []
        if hasattr(self.app, "detail_sections"):
            try:
                sections = self.app.detail_sections(provider)
            except Exception:
                logging.getLogger(__name__).exception("detail sections failed")
        self.detail_groups = detail_groups(sections)
        self._layout_detail()
        self._paint_chart(self.charts["detail"], fresh=True)

    def _layout_detail(self):
        """The card and the detail tiles at one size: as wide as an overview
        card and as tall as the tallest of them, so they read as one set."""
        found = next(((card, icon) for card, icon in self.cards if card.provider == self.detail_provider), None)
        width = view.CARD_W
        slots = (len(found[0].rows) or 1) if found else 1
        height = max([view.card_height(slots)] + [view.fact_height(rows, self.scale, width, title) for title, rows in self.detail_groups])
        tiles = []

        def tile(key, signature, draw):
            name = "detail:tile:" + key
            if self.tile_art.get(name) != signature or self.images.get(name + ":widget") is None:
                self.tile_art[name] = signature
                self.picture(self.detail_card, name, draw())
            tiles.append((key, self.images[name + ":widget"], None, width))

        if found is not None:
            card, icon = found
            tile("card", (card, id(icon), height),
                 lambda: view.source_card(card, icon, self.scale * self.f, slots=slots, width=width, height=height))
        for index, (title, rows) in enumerate(self.detail_groups):
            tile(str(index), (title, tuple(rows), height),
                 lambda title=title, rows=rows: view.fact_tile(title, rows, self.scale * self.f, width, height))
        used = {"detail:tile:" + key for key, *_ in tiles}
        for name in [name for name in self.tile_art if name not in used]:
            self.tile_art.pop(name)
            widget = self.images.pop(name + ":widget", None)
            self.images.pop(name, None)
            if widget is not None:
                widget.destroy()
        self.detail_flow.set(tiles)
        self.detail_flow.layout()
        self._paint_chart(self.charts["detail"])

    # ----- 来源 ---------------------------------------------------------------------------
    def _sources(self, page):
        import providers
        tk, S = self.tk, self.S
        self.heading(page, "来源", "开关改动后自动保存，托盘随即重启应用；MiMo 的登录与手动套餐同样立即生效。")
        if os.environ.get("CIRCLOW_PROVIDERS"):
            self.para(page, f"已设置 CIRCLOW_PROVIDERS={os.environ['CIRCLOW_PROVIDERS']}；本页开关优先于它。", 9, view.WARN).pack(
                anchor="w", fill="x", pady=(0, S(10)))
        states = {state["key"]: state for state in providers.provider_states()}
        self.source_rows = {}
        box = self.card(page)
        for index, key in enumerate(in_tray_order(states)):
            state = states[key]
            if index:
                tk.Frame(box, bg=view.BORDER, height=1).pack(fill="x", pady=S(12))
            self.vars["source:" + key] = tk.BooleanVar(value=state["enabled"])
            self.vars["source:" + key].trace_add("write", lambda *_: self.update_footer())
            row = self.setting(box, PROVIDER_NAMES.get(key, key), state["description"], self.vars["source:" + key],
                               command=self._fill_sources, badge=(state["letter"], view.hex_color(view.accent(state["letter"]))))
            row.state = self.para(row.text, "", 9, view.INK_2)
            row.state.pack(anchor="w", fill="x", pady=(self.S(4), 0))
            self.source_rows[key] = row
            if key == "mimo":
                self._mimo(box)
        self.section(page, "本机记录", "各读取器使用的本机数据")
        self.health_box = tk.Frame(page, bg=view.BG)
        self.health_box.pack(fill="x")

    def _fill_sources(self):
        import providers
        for state in providers.provider_states():
            row = self.source_rows.get(state["key"])
            if row is None:
                continue
            pending = bool(self.vars["source:" + state["key"]].get()) != state["enabled"]
            text = ("当前已开启" if state["enabled"] else "当前已关闭") + f" · {state['reason']}" + ("　→ 正在应用，托盘将重启" if pending else "")
            self.set_text(row.state, text, fg=view.WARN if pending else view.INK_2)
        self._fill_health()

    def _mimo(self, box):
        tk, S = self.tk, self.S
        holder = self.panel(box, view.FIELD, view.BORDER, padx=14, pady=12, radius=10)
        holder.pack(fill="x", pady=(S(10), 0), padx=(S(36), 0))
        block = holder.inner
        self.mimo_head = self.label(block, "", 10, weight="bold")
        self.mimo_head.pack(anchor="w")
        self.mimo_info = self.para(block, "", 9, view.INK_3)      # shown only when it has something to say
        self.mimo_warn = self.para(block, "", 9, view.WARN)
        line = tk.Frame(block, bg=view.FIELD)
        line.pack(fill="x", pady=(S(10), 0))
        self.button(line, "登录小米 MiMo", self.mimo_login, "primary").pack(side="left")
        self.button(line, "退出登录", self.mimo_logout).pack(side="left", padx=S(8))
        self.mimo_toggle = self.link(line, "手动填写套餐 ▸", self.toggle_mimo_manual)
        self.mimo_toggle.pack(side="left", padx=S(8))
        self.para(block, "登录会打开一个独立的 Edge/Chrome 窗口进入 platform.xiaomimimo.com；进入套餐页后自动读取并关闭。"
                         "会话用 Windows 加密保存在本机（mimo-session.bin），不保存账号密码。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(8), 0))
        self.mimo_drawer = tk.Frame(block, bg=view.FIELD, height=1)     # slides open around the form
        self.mimo_open = False
        self.mimo_manual = tk.Frame(self.mimo_drawer, bg=view.FIELD)
        self.mimo_manual.pack(fill="x", anchor="n")
        self.mimo_fields = {name: tk.StringVar() for name in ("total", "end", "used", "name")}
        for variable in self.mimo_fields.values():
            variable.trace_add("write", lambda *_: self.update_footer())
        self.label(self.mimo_manual, "不登录、免费额度或登录失败时使用：只统计本机用量", 9, view.INK_3).pack(anchor="w", pady=(S(10), 0))
        self.form(self.mimo_manual, ((self.mimo_fields["total"], "套餐总量", "例如 49.2B、492亿", False),
                                     (self.mimo_fields["end"], "到期日期", "YYYY-MM-DD", False),
                                     (self.mimo_fields["used"], "已用量（可选）", "控制台当前已用", False),
                                     (self.mimo_fields["name"], "套餐名（可选）", "例如 Max 年付", False)))
        buttons = tk.Frame(self.mimo_manual, bg=view.FIELD)
        buttons.pack(fill="x", pady=(S(8), 0))
        self.button(buttons, "删除手动套餐", self.remove_mimo_manual).pack(side="left")
        self.label(buttons, "填好套餐总量与到期日期后自动保存", 9, view.INK_3).pack(side="left", padx=S(12))

    def toggle_mimo_manual(self):
        drawer = self.mimo_drawer
        opening = self.mimo_open = not self.mimo_open
        self.mimo_toggle.configure(text="手动填写套餐 ▾" if opening else "手动填写套餐 ▸")
        self.mimo_manual.update_idletasks()
        full = self.mimo_manual.winfo_reqheight()
        start = drawer.winfo_height() if drawer.winfo_manager() else 0
        drawer.pack_propagate(False)
        if not drawer.winfo_manager():
            drawer.configure(height=1)
            drawer.pack(fill="x")

        def done():
            if opening:
                drawer.pack_propagate(True)
            else:
                drawer.pack_forget()
        self.motion.to("mimo-drawer", start, full if opening else 0, .28,
                       lambda height: drawer.configure(height=max(1, round(height))), done=done)

    def _fill_mimo(self):
        info = self.app.mimo_status() if hasattr(self.app, "mimo_status") else {}
        manual = info.get("manual")
        if info.get("logged_in"):
            head, color = "✓ 已登录控制台", view.GREEN
        elif manual is not None:
            head, color = "✓ 使用手动套餐", view.GREEN
        else:
            head, color = "尚未配置：登录控制台，或手动填写套餐", view.WARN
        total, used = info.get("total"), info.get("used")
        parts = []
        if total:
            parts.append(f"套餐 {info.get('plan') or ''} 共 {total / 1e9:.2f}B" + (f"，已用 {used / 1e9:.2f}B" if used is not None else ""))
        if not info.get("browser"):
            parts.append("未找到 Edge/Chrome：登录需要其中之一，或设置 CIRCLOW_MIMO_BROWSER 指向浏览器")
        warn = info.get("login_result") or info.get("error") or ""
        for widget, text, ink in ((self.mimo_head, head, color), (self.mimo_info, " · ".join(parts), view.INK_3),
                                  (self.mimo_warn, warn, view.WARN)):
            self.set_text(widget, text, fg=ink)
        for widget, after in ((self.mimo_info, self.mimo_head), (self.mimo_warn, self.mimo_info)):
            if widget.cget("text") and not widget.winfo_manager():
                widget.pack(anchor="w", fill="x", after=after)
            elif not widget.cget("text") and widget.winfo_manager():
                widget.pack_forget()
        if manual is not None and not self.mimo_open and not info.get("logged_in"):
            self.toggle_mimo_manual()

    def mimo_login(self):
        self.app.mimo_login()
        self.root.after(300, self._fill_mimo)

    def mimo_logout(self):
        self.app.mimo_logout()
        self._fill_mimo()

    def remove_mimo_manual(self):
        import mimo_usage
        mimo_usage.remove_manual_plan()
        # Empty the form as the saved state first, so clearing it is not an edit to save.
        self.origins.update({"mimo:" + name: "" for name in self.mimo_fields})
        getattr(self, "save_problems", {}).pop("mimo", None)
        for variable in self.mimo_fields.values():
            variable.set("")
        self.say("已删除手动套餐")
        self._fill_mimo()

    def _fill_health(self):
        tk, S = self.tk, self.S
        health = (getattr(self.app, "performance", None) or {}).get("source_health", {}) or {}
        signature = tuple(sorted((name, str((value or {}).get("status")), str((value or {}).get("sessions")))
                                 for name, value in health.items()))
        if self.images.get("health:signature") == signature:
            return
        self.images["health:signature"] = signature
        for child in self.health_box.winfo_children():
            child.destroy()
        tones = {"ok": (view.GREEN, "#23342c"), "error": ("#ef8a7f", "#3a2524"), "idle": (view.INK_3, view.SURFACE_HI)}
        flow = FlowGrid(self, self.health_box, gap=8)
        tiles = []
        for name, value in sorted(health.items()):
            status = str((value or {}).get("status") or "未知")
            ink, fill = tones[view.health_tone(status)]
            tile = self.panel(flow.frame, padx=12, pady=9, radius=10)
            chip = tile.inner
            tiles.append((name, tile, None, 200))
            self.label(chip, name, 10, weight="bold").pack(anchor="w")
            line = tk.Frame(chip, bg=view.SURFACE)
            line.pack(anchor="w", pady=(S(3), 0))
            self.pill(line, status, fill, ink).pack(side="left")
            extra = value.get("sessions") if isinstance(value, dict) else None
            if extra is not None:
                self.label(line, f"  {extra} 个会话", 8, view.INK_3).pack(side="left")
        flow.set(tiles)
        flow.frame.after_idle(flow.layout)
        if not health:
            self.label(self.health_box, "正在收集…", 10, view.INK_3).pack(anchor="w")

    # ----- 模型智商 -------------------------------------------------------------------------
    def _iq(self, page):
        import grok_iq
        import model_iq
        tk, S = self.tk, self.S
        self.heading(page, "模型智商", "面板横条显示所选模型的编程分，Grok 栏显示它相对最强模型的总分。")
        self.section(page, "面板横条", "AI Stupid Level · 近 24 小时编程分（0–100）")
        box = self.card(page)
        self.iq_key = tk.StringVar()
        self.iq_key.trace_add("write", lambda *_: self.update_footer())
        self.iq_built = None
        self.iq_checks = {}
        self.form(box, ((self.iq_key, "API Key", "asl_live_…，仅保存在本机", True),))
        line = tk.Frame(box, bg=view.SURFACE)
        line.pack(fill="x", pady=(S(10), 0))
        self.button(line, "获取免费 Key", lambda: os.startfile(model_iq.SIGNUP)).pack(side="left")  # type: ignore[attr-defined]
        self.button(line, "立即读取", self.iq_refresh, "primary").pack(side="left", padx=S(8))
        self.iq_message = tk.StringVar()
        self.label(line, "", 9, view.INK_2, textvariable=self.iq_message).pack(side="left", padx=S(8))
        self.iq_info = self.para(box, "", 9, view.INK_3)
        self.iq_info.pack(anchor="w", fill="x", pady=(S(8), 0))
        self.para(box, "每 3 小时自动读取一次；免费 Key 每天 10 次，自动最多用 7 次，其余留给“立即读取”。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(2), 0))
        self.section(page, "Grok 智商栏", "LiveBench · 以榜上最高分为 100%")
        grok = self.card(page)
        top = tk.Frame(grok, bg=view.SURFACE)
        top.pack(fill="x")
        self.grok_figure = self.label(top, "—", 22, view.INK, weight="bold")
        self.grok_figure.pack(side="left")
        words = tk.Frame(top, bg=view.SURFACE)
        words.pack(side="left", padx=S(14))
        self.grok_caption = self.label(words, "", 11, weight="bold")
        self.grok_caption.pack(anchor="w")
        self.grok_info = self.label(words, "", 9, view.INK_3)
        self.grok_info.pack(anchor="w")
        actions = tk.Frame(top, bg=view.SURFACE)
        actions.pack(side="right")
        self.button(actions, "立即读取", self.grok_refresh).pack(side="right")
        self.grok_message = tk.StringVar()
        self.label(actions, "", 9, view.INK_2, textvariable=self.grok_message).pack(side="right", padx=S(8))
        self.para(grok, "Grok 不在 AI Stupid Level 的榜单上，所以改用 LiveBench 最新公开榜单的加权总分；"
                        "每 3 小时检查一次，只有出现新榜单时才重新下载。", 9, view.INK_3).pack(anchor="w", fill="x", pady=(S(10), 0))
        self.link(grok, "数据来源：livebench.ai", lambda: os.startfile(grok_iq.SITE)).pack(anchor="w", pady=(S(6), 0))  # type: ignore[attr-defined]

        pick = self.section(page, "横条显示的模型")
        self.button(pick, "恢复默认", self.iq_default).pack(side="right")
        self.iq_pick = self.label(pick, "", 9, view.INK_3)
        self.iq_pick.pack(side="left", padx=S(10))
        table = self.panel(page, padx=14, pady=10)
        table.pack(fill="x")
        self.iq_table = table.inner
        source = self.link(page, "数据来源：aistupidlevel.info（AI Stupid Level）", lambda: os.startfile(model_iq.SITE))  # type: ignore[attr-defined]
        source.pack(anchor="w", pady=(S(8), 0))

    def iq_refresh(self):
        if not hasattr(self.app, "refresh_model_iq"):
            return
        try:
            self.app.save_model_iq(self.iq_key.get(), list(self.iq_selected) or None)
        except (OSError, ValueError) as error:
            self.messagebox.showerror("未能保存", str(error), parent=self.root)
            return
        self.iq_message.set("正在读取…")

        def worker():
            try:
                text = self.app.refresh_model_iq()
            except Exception as error:
                text = "读取失败：" + type(error).__name__
            self.commands.put(("iq", text))
        threading.Thread(target=worker, name="model-iq-manual", daemon=True).start()

    def grok_refresh(self):
        if not hasattr(self.app, "refresh_grok_iq"):
            return
        self.grok_message.set("正在读取…")

        def worker():
            try:
                text = self.app.refresh_grok_iq()
            except Exception as error:
                text = "读取失败：" + type(error).__name__
            self.commands.put(("grok", text))
        threading.Thread(target=worker, name="grok-iq-manual", daemon=True).start()

    def iq_default(self):
        self.iq_selected = []
        for var in self.iq_checks.values():
            var.set(False)
        self._fill_iq(rebuild=False)
        self.update_footer()

    def iq_toggle(self, name):
        import model_iq
        var = self.iq_checks[name]
        if var.get():
            if len(self.iq_selected) >= model_iq.HOVER_MAX:
                var.set(False)
                self.iq_message.set(f"横条最多显示 {model_iq.HOVER_MAX} 个模型")
                return
            self.iq_selected.append(name)
        elif name in self.iq_selected:
            self.iq_selected.remove(name)
        self._fill_iq(rebuild=False)
        self.update_footer()

    def _fill_iq(self, rebuild=True):
        import grok_iq
        import model_iq
        tk, S = self.tk, self.S
        state = getattr(self.app, "grok_iq", None)
        _label, figure, caption, unit = grok_iq.column(state)
        info = []
        if state is not None and state.release:
            info.append(f"榜单 {state.release}")
        if state is not None and state.fetched_at:
            info.append("上次读取 " + time.strftime("%m-%d %H:%M", time.localtime(state.fetched_at)))
        if state is not None and state.error:
            info.append(state.error)
        for widget, text in ((self.grok_figure, figure + unit), (self.grok_caption, caption),
                             (self.grok_info, " · ".join(info) or "尚未读取")):
            if widget.cget("text") != text:
                widget.configure(text=text)
        if not hasattr(self.app, "model_iq_status"):
            return
        status = self.app.model_iq_status()
        parts = [f"上次读取：{status['fetched']}", f"今日已用 {status['calls']} / {model_iq.DAILY_LIMIT} 次（00:00 UTC 重置）"]
        if status["env_key"]:
            parts.append(f"已设置 {model_iq.ENV_KEY} 环境变量，优先于本页的 Key")
        if status["error"]:
            parts.append("⚠ " + status["error"])
        self.set_text(self.iq_info, " · ".join(parts))
        shown = self.iq_selected or status["default"]
        pick = ("、".join(model_iq.short_name(n) for n in shown) or "—") + \
            ("（默认：编程分最高的 3 个，正在运行的模型始终在最前）" if not self.iq_selected else "") + f"；最多 {model_iq.HOVER_MAX} 个"
        if self.iq_pick.cget("text") != pick:
            self.iq_pick.configure(text=pick)
        if rebuild and self.iq_built != status["fetched_at"]:
            self.iq_built = status["fetched_at"]
            for child in self.iq_table.winfo_children():
                child.destroy()
            self.iq_checks = {}
            models = status["models"]
            if not models:
                self.label(self.iq_table, "填写 API Key 后点“立即读取”，这里会列出所有模型的分数。", 10, view.INK_3).grid(row=0, column=0, sticky="w")
                return
            for column, title in enumerate(("显示", "#", "模型", "厂商", "编程分")):
                self.label(self.iq_table, title, 9, view.INK_3).grid(row=0, column=column, sticky="w", padx=(0, S(14)), pady=(0, S(4)))
            self.iq_table.columnconfigure(2, weight=1)
            for index, score in enumerate(models, 1):
                var = self.iq_checks[score.name] = tk.BooleanVar(value=score.name in self.iq_selected)
                tk.Checkbutton(self.iq_table, variable=var, command=lambda name=score.name: self.iq_toggle(name), bg=view.SURFACE,
                               activebackground=view.SURFACE, selectcolor=view.FIELD, fg=view.INK, bd=0,
                               highlightthickness=0).grid(row=index, column=0, sticky="w")
                cells = (str(index), f"{score.short}  ({score.name})" if score.short != score.name else score.name,
                         model_iq.provider_label(score.provider), model_iq.score_text(score))
                for column, value in enumerate(cells, 1):
                    color = view.INK if column in (2, 4) else view.INK_2
                    self.label(self.iq_table, value, 10 if column in (2, 4) else 9, color,
                               weight="bold" if column == 4 else "normal").grid(row=index, column=column, sticky="w", padx=(0, S(14)))

    # ----- 任务 ---------------------------------------------------------------------------
    def _tasks(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "任务", "任务气泡来自本机 agent 记录；herdr 同一窗格的会话合并为一个气泡。")
        box = self.card(page)
        self.vars["herdr"] = tk.BooleanVar(value=True)
        self.vars["herdr"].trace_add("write", lambda *_: self.update_footer())
        self.herdr_row = self.setting(box, "herdr 适配", "", self.vars["herdr"])
        self.agent_holder = tk.Frame(page, bg=view.BG)
        self.agent_holder.pack(fill="x", pady=(0, S(14)))

        self.section(page, "任务命名", "可选：用 OpenAI 兼容接口（含 MiMo Token Plan）把请求归纳为气泡标题")
        naming = self.card(page)
        self.vars["summary"] = tk.BooleanVar()
        self.vars["summary"].trace_add("write", lambda *_: self.update_footer())
        self.setting(naming, "启用模型命名", "启用后发送每个成员最近一次请求的前 400 字、项目文件夹名和当前动作。密钥只保存在本机。",
                     self.vars["summary"])
        self.fields = {name: tk.StringVar() for name in ("url", "key", "model")}
        for variable in self.fields.values():
            variable.trace_add("write", lambda *_: self.update_footer())
        self.form(naming, ((self.fields["url"], "API 地址", "https://…/v1", False), (self.fields["key"], "API Key", "仅保存在本机", True),
                           (self.fields["model"], "模型", "例如 mimo-v2-flash", False)))
        test = tk.Frame(naming, bg=view.SURFACE)
        test.pack(fill="x", pady=(S(10), 0))
        self.test_button = self.button(test, "测试连接", self.test_connection)
        self.test_button.pack(side="left")
        self.result = tk.StringVar()
        self.label(test, "", 10, view.INK_2, textvariable=self.result).pack(side="left", padx=S(12))
        if any(os.environ.get("CIRCLOW_SUMMARY_" + name) for name in ("URL", "KEY", "MODEL")):
            self.para(naming, "已设置 CIRCLOW_SUMMARY_* 环境变量，环境变量优先于本页配置。", 9, view.WARN).pack(
                anchor="w", fill="x", pady=(S(10), 0))

    def _fill_tasks(self):
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        if herdr is not None:
            env = os.environ.get("CIRCLOW_HERDR")
            text = (f"状态：{herdr.status} · {len(herdr.rows)} 个窗格。同一窗格的会话合并为一个气泡，blocked 显示为“等你确认”；"
                    "点击 Grok 图标会在 herdr 新建工作区启动它；Claude 图标打开 Claude App，没装 App 时才在 herdr 启动 Claude Code。")
            if env:
                text += f"（环境变量 CIRCLOW_HERDR={env} 优先）"
            self.set_text(self.herdr_row.detail, text)
        letters = {provider: letter_of(provider) for provider in PROVIDER_NAMES}
        agents = [agent for agent in (getattr(self.app, "agents", None) or []) if not agent.get("schedule")]
        width = round(self.page_width("tasks"))
        widget = self.picture(self.agent_holder, "agents", view.agent_rows(agents, letters, self.scale * self.f, width=max(400, width)))
        if not widget.winfo_manager():
            widget.pack(anchor="w")

    # ----- 常规 ---------------------------------------------------------------------------
    def _general(self, page):
        import providers
        import refresh_scheduler
        tk, S = self.tk, self.S
        self.heading(page, "常规", "开机自启、额度读取节奏、工具与退出。")
        box = self.card(page)
        self.vars["startup"] = tk.BooleanVar()
        self.vars["startup"].trace_add("write", lambda *_: self.update_footer())
        self.setting(box, "开机自启", "登录 macOS 后自动在菜单栏显示。" if sys.platform == "darwin"
                     else "登录 Windows 后自动在托盘显示。", self.vars["startup"])

        self.section(page, "额度读取节奏", "光标停在图标或面板上最快，正在使用其次，静默最慢")
        cadence = self.card(page)
        grid = tk.Frame(cadence, bg=view.SURFACE)
        grid.pack(fill="x")
        for column, title in enumerate(("来源", "静默", "使用中", "光标停留", "下次读取")):
            self.label(grid, title, 9, view.INK_3).grid(row=0, column=column, sticky="w", padx=(0, S(20)), pady=(0, S(6)))
        grid.columnconfigure(4, weight=1)
        self.cadence_next = {}
        for row, key in enumerate(in_tray_order([spec.key for spec in providers.SPECS]), 1):
            letter = letter_of(key)
            name = tk.Frame(grid, bg=view.SURFACE)
            name.grid(row=row, column=0, sticky="w", padx=(0, S(20)), pady=S(3))
            self.badge(name, letter, view.hex_color(view.accent(letter)), size=20).pack(side="left")
            self.label(name, PROVIDER_NAMES.get(key, key), 10).pack(side="left", padx=(S(8), 0))
            for column, table in enumerate((refresh_scheduler.IDLE, refresh_scheduler.ACTIVE, refresh_scheduler.HOVER), 1):
                self.label(grid, interval_text(table[key]), 10, view.INK_2).grid(row=row, column=column, sticky="w", padx=(0, S(20)))
            self.cadence_next[key] = self.label(grid, "", 9, view.INK_3)
            self.cadence_next[key].grid(row=row, column=4, sticky="w")
        self.para(cadence, "Grok 每次读取要开一个 CLI 会话，所以间隔最长；任何来源两次读取之间至少隔开上一次读取用时的 2 倍。"
                           "读取失败按 30 秒、1、2、5 分钟退避；生成刚结束或重置时间一到会提前读取。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(10), 0))

        self.section(page, "多端同步", "经云服务器与其他设备共享额度读数、用量汇总和每周摘要（只有数字）")
        sync = self.card(page)
        self.sync_state = self.label(sync, "", 10, view.INK_2)
        self.sync_state.pack(anchor="w")
        self.para(sync, "在数据文件夹的 sync.json 里填写服务器地址与令牌即可开启；把同一份 sync.json 放到另一台电脑的数据文件夹，"
                        "那台电脑就会加入。服务器暂停或冷却时各自照常运行，连上后自动补同步。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(6), 0))

        self.section(page, "工具", "")
        tools = self.card(page)
        line = tk.Frame(tools, bg=view.SURFACE)
        line.pack(fill="x")
        for text, command in (("诊断信息", self.app.open_diagnostics), ("全局重置预测", self.app.open_reset_page),
                              ("全部 AI 速率", self.app.open_all_performance), ("打开数据文件夹", self.open_data_folder)):
            self.button(line, text, command).pack(side="left", padx=(0, S(8)))
        danger = self.card(page)
        self.button(danger, "退出全部", self.app.exit_app, "danger").pack(side="right")
        self.label(danger, "退出", 11, weight="bold").pack(anchor="w")
        self.label(danger, "关闭全部托盘图标与后台读取。", 9, view.INK_3).pack(anchor="w")

    def _fill_general(self):
        scheduler = getattr(self.app, "scheduler", None)
        now = time.time()
        status = getattr(self.app, "sync_status", None)
        self.set_text(self.sync_state, status() if status else "未开启")
        for key, widget in self.cadence_next.items():
            text = "未开启"
            if scheduler is not None and key in getattr(scheduler, "providers", ()):
                text = view.refresh_text(*scheduler.plan(key, now)) or "等待"
            if widget.cget("text") != text:
                widget.configure(text=text)

    # ----- live content --------------------------------------------------------------------
    def refresh_live(self):
        if self.root.state() == "withdrawn":
            return
        try:
            {"overview": self._fill_overview, "detail": self._fill_detail, "sources": self._fill_sources_all,
             "iq": self._fill_iq, "tasks": self._fill_tasks, "general": self._fill_general}[self.page]()
        except Exception:
            logging.getLogger(__name__).exception("settings refresh failed")
        self.update_footer()

    def _fill_sources_all(self):
        self._fill_sources()
        self._fill_mimo()

    # ----- staged changes ------------------------------------------------------------------------
    def staged(self) -> dict:
        values = {name: bool(var.get()) for name, var in self.vars.items()}
        values.update({"field:" + name: var.get().strip() for name, var in self.fields.items()})
        values["iq:key"] = self.iq_key.get().strip()
        values["iq:shown"] = tuple(self.iq_selected)
        values.update({"mimo:" + name: var.get().strip() for name, var in getattr(self, "mimo_fields", {}).items()})
        return values

    def changed_keys(self) -> list[str]:
        """Staged keys whose value differs from what is saved."""
        return [name for name, value in self.staged().items() if self.origins.get(name, value) != value]

    def pending(self) -> list[str]:
        labels = {"herdr": "herdr 适配", "startup": "开机自启", "summary": "任务命名", "iq:key": "智商 API Key",
                  "iq:shown": "横条显示的模型"}
        changed, seen = [], set()
        for name in self.changed_keys():
            if name.startswith("source:"):
                text = PROVIDER_NAMES.get(name[7:], name[7:]) + " 开关"
            elif name.startswith("field:"):
                text = "任务命名"
            elif name.startswith("mimo:"):
                text = "手动套餐"
            else:
                text = labels.get(name, name)
            if text not in seen:
                seen.add(text)
                changed.append(text)
        return changed

    def say(self, text: str):
        self.notice = (text, time.time())
        self.update_footer()

    def update_footer(self):
        """Runs on every change: a value that differs from the last one seen
        schedules the automatic save; the footer shows its outcome."""
        if not hasattr(self, "status_label") or not self.origins:
            return
        staged = self.staged()
        if staged != getattr(self, "seen", None):
            self.seen = staged
            keys = self.changed_keys()
            if keys:
                self.schedule_save(save_delay(keys))
        notice, at = self.notice
        problems = getattr(self, "save_problems", {})
        if problems:
            text, color = "未能保存：" + "；".join(problems.values()), view.WARN
        elif notice and time.time() - at < NOTICE_SECONDS:
            text, color = notice, view.GREEN if notice.startswith("✓") else view.INK_2
        else:
            text, color = "", view.INK_2
        if self.status.get() != text:
            self.status.set(text)
        if self.status_label.cget("fg") != color:
            self.status_label.configure(fg=color)

    def schedule_save(self, delay_ms: int):
        job = getattr(self, "save_job", None)
        if job is not None:
            self.root.after_cancel(job)
        self.save_job = self.root.after(delay_ms, self.autosave)

    def autosave(self):
        self.save_job = None
        self.save()

    # ----- actions -------------------------------------------------------------------------------
    def open_data_folder(self):
        folder = agent_tasks.SUMMARY_CONFIG.parent
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)  # noqa: S606 - user-requested local folder

    def test_connection(self):
        config = {name: value.get().strip() for name, value in self.fields.items()}
        address = urlsplit(config["url"])
        if address.scheme not in ("http", "https") or not address.hostname or not config["model"]:
            self.result.set("请先填写 API 地址与模型")
            return
        config["url"] = config["url"].rstrip("/")
        if not config["url"].endswith("/chat/completions"):
            config["url"] += "/chat/completions"
        self.result.set("正在测试连接…")

        def worker():
            try:
                cluster = agent_tasks.Cluster("settings-test", ({"key": "settings-test", "request": "测试任务命名连接", "title": "连接测试", "doing": "检查接口"},))
                reply = agent_tasks.Summarizer(config).request(cluster)
                text = "连接成功，已收到任务名称" if reply.get("title") else "API 已响应，但没有返回有效任务名称"
            except Exception as error:
                # Do not expose endpoint responses or credentials in dialogs/logs.
                text = "连接失败：" + type(error).__name__ + (f"（HTTP {error.code}）" if hasattr(error, "code") else "")
            self.commands.put(("result", text))
        threading.Thread(target=worker, name="summary-test", daemon=True).start()

    def load(self):
        import providers
        config = load_summary()
        for name, value in self.fields.items():
            value.set(config[name])
        self.vars["summary"].set(config["enabled"])
        self.vars["startup"].set(self.app.startup_enabled())
        for state in providers.provider_states():
            if "source:" + state["key"] in self.vars:
                self.vars["source:" + state["key"]].set(state["enabled"])
        manual = {}
        try:
            import mimo_usage
            manual = json.loads(mimo_usage.CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, ImportError):
            pass
        for name, key in (("total", "total_credits"), ("end", "period_end"), ("used", "used_credits"), ("name", "plan_name")):
            value = manual.get(key, "") if isinstance(manual, dict) else ""
            self.mimo_fields[name].set(f"{value / 1e9:g}B" if name in ("total", "used") and isinstance(value, (int, float)) else str(value or ""))
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        self.vars["herdr"].set(bool(herdr.enabled) if herdr is not None else True)
        if hasattr(self.app, "model_iq_status"):
            info = self.app.model_iq_status()
            self.iq_key.set(info["key"])
            self.iq_selected = list(info["shown"] or [])
            self.iq_built = None
        job = getattr(self, "save_job", None)
        if job is not None:
            self.root.after_cancel(job)
            self.save_job = None
        self.origins = self.staged()
        self.seen = dict(self.origins)            # loading is not an edit: nothing to save
        self.save_problems = {}
        self.notice = ("", 0.0)
        self.result.set("")
        self.update_footer()

    def save(self):
        """Save every changed group on its own (called a moment after an edit,
        and on closing). A group that fails keeps its changes pending and its
        reason in the footer, without holding back the others; it is tried
        again on its next edit. Source switches go last: applying them
        restarts the tray."""
        keys = set(self.changed_keys())
        if not keys:
            return
        staged = self.staged()
        problems = dict(getattr(self, "save_problems", {}))
        saved = []

        def group(name, label, members, action):
            if not keys.intersection(members):
                return
            try:
                if action() is False:             # not ready yet (a half-filled form): stays pending, no error
                    return
            except (OSError, ValueError) as error:
                problems[name] = f"{label}：{error}"
                return
            problems.pop(name, None)
            self.origins.update({key: staged[key] for key in members if key in staged})
            saved.append(name)

        fields = ["field:" + name for name in self.fields]
        group("summary", "任务命名", ["summary"] + fields,
              lambda: self.app.apply_summary_settings({name: var.get() for name, var in self.fields.items()},
                                                      self.vars["summary"].get()))
        group("startup", "开机自启", ["startup"], self.app.toggle_startup)
        if hasattr(self.app, "save_model_iq"):
            group("iq", "智商设置", ["iq:key", "iq:shown"],
                  lambda: self.app.save_model_iq(self.iq_key.get(), list(self.iq_selected) or None))
        if hasattr(self.app, "save_preferences"):
            group("herdr", "herdr 适配", ["herdr"], lambda: self.app.save_preferences({"herdr": bool(self.vars["herdr"].get())}))
        group("mimo", "手动套餐", ["mimo:" + name for name in getattr(self, "mimo_fields", {})], self._save_mimo_manual)
        sources = {name[7:]: bool(var.get()) for name, var in self.vars.items()
                   if name.startswith("source:") and bool(var.get()) != self.origins.get(name)}
        self.save_problems = problems
        if sources:
            group("sources", "来源开关", ["source:" + key for key in sources], lambda: self.app.set_providers(sources))
        if "sources" in saved:
            self.say("✓ 已保存，托盘正在重启以应用来源开关…")
        elif saved:
            self.say("✓ 已自动保存")
        else:
            self.update_footer()

    def _save_mimo_manual(self):
        """The manual MiMo plan, once its required fields are filled."""
        values = {name: var.get().strip() for name, var in self.mimo_fields.items()}
        if not values["total"] or not values["end"]:
            return False
        self.app.save_mimo_manual(values["total"], values["end"], values["used"], values["name"])
        self._fill_mimo()
        return True

    def close(self):
        """Save what is still waiting, then hide the window. A change that
        could not be saved is named first; closing anyway is the person's call."""
        job = getattr(self, "save_job", None)
        if job is not None:
            self.root.after_cancel(job)
            self.save_job = None
        if self.origins:
            self.save()
        problems = getattr(self, "save_problems", {})
        if problems and not self.messagebox.askyesno("有设置未能保存", "\n".join(problems.values()) + "\n\n仍然关闭？",
                                                     parent=self.root):
            return
        self.root.withdraw()

    # ----- loop ------------------------------------------------------------------------------------
    def appear(self, page=None):
        """Show the window (fading in when it was hidden), optionally on a page."""
        hidden = self.root.state() == "withdrawn"
        if hidden:
            self.load()
            if self.motion.enabled:
                self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        if hidden:
            ui_motion.dark_title_bar(self.root, view.SIDEBAR, view.INK, view.BORDER)
        self.root.lift()
        if page:
            self.show_page(page, slide="rise" if not hidden else None)
        self.root.update_idletasks()
        self._place_nav(animate=False)
        self.refresh_live()
        if hidden and self.motion.enabled:
            self.motion.run("appear", .2, lambda value: self.root.attributes("-alpha", value))

    def run(self):
        self.ticks = 0

        def poll():
            if self.app.stop_event.is_set():
                self.root.destroy()
                return
            while True:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                if command[0] == "close":
                    self.root.destroy()
                    return
                if command[0] == "show":
                    self.appear(command[1] if len(command) > 1 else None)
                elif command[0] == "result":
                    self.result.set(command[1])
                elif command[0] == "iq":
                    self.iq_message.set(command[1])
                    self.iq_built = None
                    self._fill_iq()
                elif command[0] == "grok":
                    self.grok_message.set(command[1])
                    self._fill_iq(rebuild=False)
            self.ticks += 1
            if self.ticks % 20 == 0:          # live figures every 2 s
                self.refresh_live()
            self.root.after(100, poll)

        poll()
        self.root.mainloop()
