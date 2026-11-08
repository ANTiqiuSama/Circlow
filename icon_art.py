"""Tray icon artwork.

One charcoal disc per source, the provider letter in the middle:

* Quota   - the ring around the letter shows what is left. One window sweeps
            clockwise from 12 o'clock. Two windows split the ring: the left
            half is the short window (5 h), the right half the long one
            (weekly), both filling from the top down. Gemini's three model
            pools take a third each (Argon top-left, Claude/GPT bottom, Gemini
            top-right), every arc growing from the middle of its third. Colours
            match the panel's columns and stay the same at any level.
* Waiting - a quota is used up: an empty ring with the time until it can run
            again in red, its unit (d / h / m) in the top-right corner.
* Live    - while a model is generating, the disc lights up in the provider's
            accent colour and carries the current TPS number.

Corner badges (only these three):
* amber dot, top-right  - stale: the last read failed, the figures are older
                          (the hover panel names the time and the reason).
* sky-blue dot, top-left - a global Codex reset looks likely.
* d / h / m, top-right   - the unit of a waiting countdown (takes the corner,
                          so a waiting icon shows no stale dot).

Letters and numerals share one serif family (Sitka Small, falling back to
Cambria / Palatino / Georgia). Everything is drawn at 4x on a 256 px canvas and
reduced once with Lanczos.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from providers import SPECS

SIZE = 64            # Pixel size handed to pystray (tests and Shell expect 64).
CANVAS = 256         # Supersampled drawing canvas.

# Warm, low-chroma palette: ivory on charcoal, accents kept quiet.
INK = (38, 37, 34, 255)            # #262622 disc
IVORY = (240, 238, 230, 255)       # #F0EEE6 letters


def _mix(base, top, amount):
    """Opaque blend; PIL draw calls replace pixels rather than compositing."""
    return tuple(round(b + (t - b) * amount) for b, t in zip(base[:3], top[:3])) + (255,)


TRACK = _mix(INK, IVORY, 0.17)     # unfilled ring
MUTED = (164, 159, 148, 255)       # unknown state
AMBER = (229, 168, 75, 255)        # < 50 %
ROSE = (226, 92, 92, 255)          # < 20 %  (clay now belongs to Claude's accent)
CLAY = ROSE                        # legacy name for the danger colour
# Accents live in the provider registry; letters map to them.
ACCENTS = {spec.letter: spec.accent for spec in SPECS}
TILE_TEXT = (31, 30, 28, 255)      # dark numerals on a lit tile
# One hue per source. Its windows run light -> deep from the one that runs
# out first to the one that lasts longest: the 5-hour window before the week.
# Codex and Gemini step down only slightly, so their windows read as one source.
WINDOW_SHADES = {"A": ((240, 196, 150, 255), ACCENTS["A"]),       # peach, Claude clay
                 "C": (ACCENTS["C"], (100, 178, 142, 255))}        # mint, a shade darker
# Gemini's pools in panel order (Argon, Claude/GPT, Gemini). Claude/GPT runs
# out first (its periwinkle), then Argon, then Gemini, each a little darker.
POOL_SHADES = ((148, 162, 240, 255), ACCENTS["G"], (130, 146, 234, 255))
PAID = (247, 201, 72, 255)


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs += [Path("/usr/share/fonts/msttcore"), Path("/usr/share/fonts/liberation-serif"),
             Path("/usr/share/fonts/liberation-sans"), Path("/usr/share/fonts/google-noto"),
             Path("/System/Library/Fonts/Supplemental")]   # macOS
    return dirs


def _load(candidates: tuple[tuple[str, str | None], ...], size: int):
    for name, variation in candidates:
        for folder in _font_dirs():
            path = folder / name
            if not path.is_file():
                continue
            try:
                face = ImageFont.truetype(str(path), size)
                if variation:
                    face.set_variation_by_name(variation)
                return face
            except (OSError, ValueError):
                continue
    return ImageFont.load_default(size)


# One elegant serif family for letters and numerals so both states read as a
# set.  Sitka (Windows 10+) has an optical "Small" cut drawn for tiny sizes;
# Cambria / Palatino / Georgia are fallbacks shipped with every Windows.
# All of these use lining figures except Georgia, which is therefore last.
SERIF = (
    ("SitkaVF.ttf", "Small Bold"),
    ("SitkaB.ttc", None),
    ("cambriab.ttf", None),
    ("palab.ttf", None),
    ("georgiab.ttf", None),
    ("Times New Roman Bold.ttf", None),   # macOS: lining figures, so ahead of Georgia
    ("Georgia Bold.ttf", None),
    ("LiberationSerif-Bold.ttf", None),
    ("LiberationSans-Bold.ttf", None),
)


@lru_cache(maxsize=64)
def letter_font(size: int):
    return _load(SERIF, size)


@lru_cache(maxsize=64)
def number_font(size: int):
    return _load(SERIF, size)


def accent(letter: str) -> tuple[int, int, int, int]:
    return ACCENTS.get(letter, IVORY)


# Sources whose numbers stay in the panel's text colour at every level
# (Gemini's pools often sit below 50 % and would read as a constant alarm).
UNIFORM_LETTERS = {"G"}


def window_shade(letter: str, slot: int, value: float | None, pooled: bool = True) -> tuple[int, int, int, int]:
    """Colour of one quota window wherever it is drawn: its ring arc, its
    panel bar, its chart line and its settings bar. A window keeps its colour
    at every level; how low it is shows in its length and in the number."""
    if value is None:
        return MUTED
    if pooled and letter in POOL_LETTERS:
        return POOL_SHADES[slot % 3]
    if pooled and letter in WINDOW_SHADES:
        return WINDOW_SHADES[letter][slot % 2]
    return accent(letter)


POOL_LETTERS = {"G"}


def ring_color(percent: float | None, letter: str) -> tuple[int, int, int, int]:
    """Warning ink for a quota number: amber under 50 %, rose under 20 %."""
    if percent is None:
        return MUTED
    if letter in UNIFORM_LETTERS:
        return accent(letter)
    if percent >= 50:
        return accent(letter)
    if percent >= 20:
        return AMBER
    return CLAY


def _centered_text(draw: ImageDraw.ImageDraw, text: str, face, box, fill, optical_dy: float = 0.0) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2 + optical_dy), text, font=face, fill=fill)


def _fit(text: str, loader, max_w: float, max_h: float, start: int, draw) -> ImageFont.FreeTypeFont:
    for size in range(start, 24, -4):
        face = loader(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
        if right - left <= max_w and bottom - top <= max_h:
            return face
    return loader(24)


def _finish(canvas: Image.Image) -> Image.Image:
    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def _stale_badge(canvas: Image.Image) -> None:
    """Amber dot at the top-right with a transparent knock-out gap."""
    cx, cy, r, gap = 214, 42, 36, 14
    knock = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(knock).ellipse((cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap), fill=255)
    alpha = canvas.getchannel("A")
    alpha.paste(0, (0, 0), knock)
    canvas.putalpha(alpha)
    ImageDraw.Draw(canvas).ellipse((cx - r, cy - r, cx + r, cy + r), fill=_ink(AMBER))


# Shared geometry: both states are full-bleed discs so the icon uses the
# whole tray slot.  The ring hugs the edge; the glyph gets the rest.
DISC = (0, 0, CANVAS - 1, CANVAS - 1)
RING_W = 32
# macOS menu bar: a template image, which the system draws in the bar's own
# label colour (white on a dark bar, black on a light one, inverted while the
# item is highlighted), so only the alpha counts: no disc, solid quota arcs
# and glyph, a dim empty track; a generating source is a solid disc with the
# number knocked out of it. Everything else keeps the coloured discs.
MONO = False
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)
MONO_TRACK = (255, 255, 255, 84)      # the empty track at a third
MONO_MUTED = (255, 255, 255, 150)     # the "?" of an unknown reading


def set_mono(on: bool) -> None:
    global MONO
    if MONO != bool(on):
        MONO = bool(on)
        for cached in (quota_icon, live_icon, ring_icon, waiting_icon):
            cached.cache_clear()


def _ink(color):
    """A colour, or solid white in mono mode (the template's shape)."""
    return WHITE if MONO else color


def _disc(draw: ImageDraw.ImageDraw, fill=INK) -> None:
    """The full-bleed disc; none in mono mode, where the bar is the ground."""
    if not MONO:
        draw.ellipse(DISC, fill=fill)


def _track():
    return MONO_TRACK if MONO else TRACK


RING_BOX = (RING_W / 2 + 1, RING_W / 2 + 1, CANVAS - RING_W / 2 - 2, CANVAS - RING_W / 2 - 2)


def _ring(draw: ImageDraw.ImageDraw, value: float | None, color, track) -> None:
    box, width = RING_BOX, RING_W
    # PIL strokes ellipses inward from the box, so expand by half the width.
    outer = (box[0] - width / 2, box[1] - width / 2, box[2] + width / 2, box[3] + width / 2)
    draw.ellipse(outer, outline=track, width=width)
    if value is None:
        return
    value = max(0.0, min(100.0, value))
    if value >= 99.5:
        draw.ellipse(outer, outline=color, width=width)
        return
    if value <= 0:
        return
    sweep = max(12.0, 360.0 * value / 100.0)
    start, end = -90.0, -90.0 + sweep
    draw.arc(outer, start, end, fill=color, width=width)
    radius = (box[2] - box[0]) / 2
    cx = (box[0] + box[2]) / 2
    for angle in (start, end):  # round caps
        x = cx + radius * math.cos(math.radians(angle))
        y = cx + radius * math.sin(math.radians(angle))
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)


def _pace_tick(draw: ImageDraw.ImageDraw, marker: float) -> None:
    """A short ivory notch across the ring: where the arc 'should' end now.
    An arc that stops before the notch means spending faster than planned."""
    angle = math.radians(-90.0 + 360.0 * max(0.0, min(100.0, marker)) / 100.0)
    radius = (RING_BOX[2] - RING_BOX[0]) / 2
    centre = (RING_BOX[0] + RING_BOX[2]) / 2
    inner, outer = radius - RING_W * 0.5, radius + RING_W * 0.62
    points = [(centre + r * math.cos(angle), centre + r * math.sin(angle)) for r in (inner, outer)]
    draw.line(points, fill=CLEAR if MONO else INK, width=30)     # in mono the notch shows the bar
    draw.line(points, fill=IVORY, width=13)


RESET_BLUE = (112, 182, 255, 255)   # "a global reset looks likely" badge


def _reset_badge(canvas: Image.Image) -> None:
    """Sky-blue dot at the top-left (opposite the stale dot), knocked out."""
    cx, cy, r, gap = 42, 42, 38, 14
    knock = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(knock).ellipse((cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap), fill=255)
    alpha = canvas.getchannel("A")
    alpha.paste(0, (0, 0), knock)
    canvas.putalpha(alpha)
    ImageDraw.Draw(canvas).ellipse((cx - r, cy - r, cx + r, cy + r), fill=_ink(RESET_BLUE))


@lru_cache(maxsize=128)
def quota_icon(remaining: float | None, letter: str, stale: bool = False, marker: float | None = None,
               reset_hint: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _disc(draw)
    value = None if remaining is None else max(0.0, min(100.0, remaining))
    _ring(draw, value, _ink(window_shade(letter, 0, value, False)), _track())
    if marker is not None and value is not None:
        _pace_tick(draw, marker)

    label, fill = (letter, _ink(IVORY)) if remaining is not None else ("?", MONO_MUTED if MONO else MUTED)
    _centered_text(draw, label, _glyph_face(label, draw), (0, 0, CANVAS, CANVAS), fill)
    if stale:
        _stale_badge(canvas)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


def tps_text(value: float) -> str:
    if value < 1000:
        return str(round(value))
    return f"{value / 1000:.0f}k" if value < 10_000 else "9k+"


@lru_cache(maxsize=64)
def live_icon(tps: float, letter: str, reset_hint: bool = False) -> Image.Image:
    """Same disc, lit in the provider accent, carrying the TPS number."""
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # Mono: a solid disc with the number knocked out, so the bar shows through it.
    draw.ellipse(DISC, fill=WHITE if MONO else accent(letter))
    text = tps_text(tps)
    _centered_text(draw, text, _numeral_face(text, draw), (0, 0, CANVAS, CANVAS), CLEAR if MONO else TILE_TEXT)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


# Gemini's three pools share the ring with no gap: 120 degrees each.
# PIL angles are clockwise from 3 o'clock.  Panel order is Argon, Claude/GPT
# (the 5-hour GPT pool), Gemini.  Each coloured arc grows from the middle of
# its third, so a short remainder stays in that corner:
# Argon top-left (8 o'clock to 12), GPT bottom (4 o'clock to 8),
# Gemini top-right (12 o'clock to 4).
THIRD_SPAN = 120.0
THIRD_CENTERS = (210.0, 90.0, 330.0)


def _third_segments(draw: ImageDraw.ImageDraw, outer, values: tuple, shades: tuple) -> None:
    for index, (center, value) in enumerate(zip(THIRD_CENTERS, values)):
        start = center - THIRD_SPAN / 2
        draw.arc(outer, start, start + THIRD_SPAN, fill=_track(), width=RING_W)
        if value is None or value <= 0:
            continue
        sweep = THIRD_SPAN * min(100.0, float(value)) / 100
        color = _ink(shades[index % len(shades)])
        draw.arc(outer, center - sweep / 2, center + sweep / 2, fill=color, width=RING_W)


@lru_cache(maxsize=128)
def ring_icon(values: tuple, letter: str, stale: bool = False, marker: float | None = None,
              reset_hint: bool = False, colors: tuple | None = None) -> Image.Image:
    """Keep the original disc and glyph; draw two windows in the outer ring.

    The left and right halves both fill from the top towards the bottom.  A
    short gap at each pole separates the two windows even at tray size.
    Three values (Gemini pools) split the ring into equal 120-degree thirds:
    Argon at the top-left, Claude/GPT along the bottom, Gemini at the top-right.
    """
    values = values[:3] or (None,)
    if len(values) == 1:
        return quota_icon(values[0], letter, stale, marker, reset_hint)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _disc(draw)
    outer = (0, 0, CANVAS - 1, CANVAS - 1)
    shades = colors or (POOL_SHADES if len(values) == 3 and letter in POOL_LETTERS
                        else WINDOW_SHADES.get(letter, (accent(letter), accent(letter))))
    if len(values) == 3:
        _third_segments(draw, outer, values, shades)
    else:
        for index, value in enumerate(values):
            # PIL angles increase clockwise.  The left half is drawn backwards.
            lo, hi = ((96, 264) if index == 0 else (-84, 84))
            draw.arc(outer, lo, hi, fill=_track(), width=RING_W)
            if value is None or value <= 0:
                continue
            sweep = 168 * min(100.0, float(value)) / 100
            color = _ink(shades[index])
            start, end = (hi - sweep, hi) if index == 0 else (lo, lo + sweep)
            draw.arc(outer, start, end, fill=color, width=RING_W)
    label, fill = (letter, _ink(IVORY)) if any(value is not None for value in values) else ("?", MONO_MUTED if MONO else MUTED)
    _centered_text(draw, label, _glyph_face(label, draw), (0, 0, CANVAS, CANVAS), fill)
    if stale:
        _stale_badge(canvas)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


def _glyph_face(label: str, draw):
    """Letter inside the quota ring. M is wider than C, X, G and A, so the
    shared box shrinks it. Keep M near their cap height, a step inside the ring."""
    if MONO:
        # 16-point template icons: a step inside the ring, M included (at 212
        # its serifs ran into the ring's inner edge, 192 across).
        return _fit(label, letter_font, 156 if label == "M" else 148, 136 if label == "M" else 140, 200, draw)
    if label == "M":
        return _fit(label, letter_font, 212, 168, 244, draw)
    return _fit(label, letter_font, 176, 168, 212, draw)


def _numeral_face(text: str, draw):
    """TPS numeral fit. The exhausted-quota countdown uses this same face."""
    wide = len(text) >= 3
    return _fit(text, number_font, 230 if wide else 212, 132 if wide else 176, 220, draw)


def _unit_face(draw):
    """Corner unit at the hour letter's size, so m and d are not width-shrunk."""
    return _fit("h", number_font, 96, 96, 132, draw)


def _on_digit(pixel) -> bool:
    if MONO:                         # the digit is solid white; the track behind the unit is a third
        return pixel[3] > 160
    return pixel[3] > 80 and pixel[0] > 150 and pixel[0] > pixel[1] + 30 and pixel[0] > pixel[2] + 30


def _draw_unit(canvas: Image.Image, unit: str) -> None:
    """Top-right unit. h is the reference corner.

    m is wider and d's bowl sits lower, so both shift further right and m
    sits a little higher. Pixels that would paint over a red digit stay red.
    """
    draw = ImageDraw.Draw(canvas)
    face = _unit_face(draw)
    _left, top, right, bottom = draw.textbbox((0, 0), unit, font=face)
    nudge_x, nudge_y = {"d": (24, 0), "m": (24, -12)}.get(unit, (0, 0))
    origin = ((CANVAS - 6) - right + nudge_x, 50 - (top + bottom) / 2 + nudge_y)
    if unit == "h":
        draw.text(origin, unit, font=face, fill=_ink(IVORY))
        return
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(origin, unit, font=face, fill=_ink(IVORY))
    src = layer.load()
    base = canvas.load()
    for y in range(canvas.height):
        for x in range(canvas.width):
            if src[x, y][3] and _on_digit(base[x, y]):
                src[x, y] = (0, 0, 0, 0)
    canvas.alpha_composite(layer)


@lru_cache(maxsize=64)
def waiting_icon(amount: int, letter: str, reset_hint: bool = False, stale: bool = False, unit: str = "h") -> Image.Image:
    """Empty ring with the TPS numeral in red, plus a corner unit."""
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _disc(draw)
    # Keep the empty outline as large as the original full-bleed disc.  A
    # slightly thicker stroke remains visible at 16-pixel tray size.  Use the
    # original empty-ring track rather than a full provider-coloured arc.
    draw.ellipse(DISC, outline=_track(), width=38)
    text = str(max(1, int(amount)))
    _centered_text(draw, text, _numeral_face(text, draw), (0, 0, CANVAS, CANVAS), _ink(CLAY))
    if reset_hint:
        _reset_badge(canvas)
    _draw_unit(canvas, unit if unit in ("d", "h", "m") else "h")
    return _finish(canvas)
