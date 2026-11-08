"""Shared surface for the hover panel: palette, fonts, wrapping and the
rounded card with its soft shadow. Drawn at the monitor's real DPI in pure
PIL, so it can be tested anywhere."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from icon_art import IVORY

MARGIN = 14                      # Transparent room for the soft shadow.
RADIUS = 14

SURFACE = (22, 21, 19, 250)          # hover card and task panel background
BORDER = (240, 238, 230, 30)
TRACK = (240, 238, 230, 30)
TEXT = IVORY
SECONDARY_TEXT = (196, 191, 180, 255)
MUTED_TEXT = (142, 137, 127, 255)


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs.append(Path("/usr/share/fonts/google-noto-cjk"))  # Preview hosts only.
    dirs.append(Path("/System/Library/Fonts"))             # macOS
    return dirs


def _load(candidates, size: int):
    for name, index, variation in candidates:
        for folder in _font_dirs():
            path = folder / name
            if not path.is_file():
                continue
            try:
                face = ImageFont.truetype(str(path), size, index=index)
                if variation:
                    face.set_variation_by_name(variation)
                return face
            except (OSError, ValueError):
                continue
    return ImageFont.load_default(size)


# Microsoft YaHei UI (index 1 of the collection) covers CJK and Latin alike;
# on macOS Hiragino Sans GB does (W3 is index 0, W6 index 2).
@lru_cache(maxsize=32)
def ui_font(size: int, weight: str = "regular"):
    if weight == "bold":
        return _load((("msyhbd.ttc", 1, None), ("msyhbd.ttc", 0, None), ("NotoSansSC-Medium.otf", 0, None),
                      ("Hiragino Sans GB.ttc", 2, None)), size)
    return _load((("msyh.ttc", 1, None), ("msyh.ttc", 0, None), ("NotoSansSC-Regular.otf", 0, None),
                  ("Hiragino Sans GB.ttc", 0, None)), size)


@lru_cache(maxsize=16)
def figure_font(size: int):
    return _load((("SegUIVar.ttf", 0, "Display Semibold"), ("segoeuisb.ttf", 0, None),
                  ("msyhbd.ttc", 1, None), ("NotoSansSC-Medium.otf", 0, None),
                  ("SFNS.ttf", 0, "Semibold"), ("Hiragino Sans GB.ttc", 2, None)), size)


def _wrap(draw, text: str, face, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if draw.textlength(line + char, font=face) <= width or not line:
                line += char
            else:
                lines.append(line.rstrip())
                line = char.lstrip()
        lines.append(line)
    return lines


@lru_cache(maxsize=8)
def _shadow(width: int, height: int, scale: float) -> Image.Image:
    margin = round(MARGIN * scale)
    shadow = Image.new("RGBA", (width + 2 * margin, height + 2 * margin), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin, margin + round(3 * scale), margin + width, margin + height + round(3 * scale)),
        radius=round(RADIUS * scale), fill=(0, 0, 0, 120))
    return shadow.filter(ImageFilter.GaussianBlur(round(7 * scale)))


def _compose(content: Image.Image, scale: float) -> Image.Image:
    width, height = content.size
    margin = round(MARGIN * scale)
    radius = round(RADIUS * scale)
    image = _shadow(width, height, scale).copy()
    surface = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(surface).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius,
        fill=SURFACE, outline=BORDER, width=max(1, round(scale)))
    surface.alpha_composite(content)
    image.alpha_composite(surface, (margin, margin))
    return image
