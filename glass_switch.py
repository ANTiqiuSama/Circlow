"""A Liquid Glass switch drawn with Pillow, for the Windows settings window.

macOS shows the system NSSwitch (settings_mac); this is its counterpart on
Windows, where Tk has no glass of its own. It follows the layered recipe
open-source Liquid Glass components use (Aave's "Building Glass for the
Web", LiquidGlassKit's switch, the MIT Glass Switch on 21st.dev):

- the track: a capsule with an inner shadow along the top and a faint light
  along the bottom, so it reads as a groove;
- the thumb at rest: a near-opaque white capsule with a soft drop shadow;
- the thumb pressed or travelling: it swells past the track's height and
  turns into clear glass. Beneath it is a copy of the track that the glass
  bends, magnified in the middle and pulled in at the rim like a convex lens.
  The rim splits colour slightly (dispersion);
- light: a Fresnel rim, brighter where the glass is seen edge-on, and a
  specular highlight in the upper left, drawn as separate passes.

There is no dot or glyph in the middle of the thumb. Everything is drawn
SUPERSAMPLE times larger and scaled down, so the curves are smooth.
"""
from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SUPERSAMPLE = 3
TRACK_OFF = (74, 72, 66)
TRACK_ON = (126, 201, 166)          # settings_view.GREEN
GLASS = (248, 247, 243)


def _rgb(color) -> tuple[int, int, int]:
    if isinstance(color, str):
        color = color.lstrip("#")
        return tuple(int(color[index:index + 2], 16) for index in (0, 2, 4))
    return tuple(color[:3])


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _capsule(size, box, fill=255) -> Image.Image:
    mask = Image.new("L", size, 0)
    x0, y0, x1, y1 = box
    if x1 > x0 and y1 > y0:
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=min(x1 - x0, y1 - y0) / 2, fill=fill)
    return mask


def _shrink(box, by: float):
    x0, y0, x1, y1 = box
    return (x0 + by, y0 + by, x1 - by, y1 - by)


def _scaled(image: Image.Image, k: float, cx: float, cy: float) -> Image.Image:
    """`image` magnified k times about (cx, cy), same size."""
    return image.transform(image.size, Image.AFFINE, (1 / k, 0, cx * (1 - 1 / k), 0, 1 / k, cy * (1 - 1 / k)),
                           resample=Image.BILINEAR)


def _alpha(mask: Image.Image, amount: float) -> Image.Image:
    return mask.point(lambda value: round(value * max(0.0, min(1.0, amount))))


def _paint(base: Image.Image, color, mask: Image.Image) -> None:
    base.paste(Image.new("RGB", base.size, color), (0, 0), mask)


def thumb_box(width: float, height: float, pad: float, at: float, press: float = 0.0, stretch: float = 0.0):
    """Where the thumb is drawn (pixels, before supersampling)."""
    inset = height * 0.09
    rest = height - 2 * inset
    tall = rest * (1 + 0.30 * press)          # held: a little taller than the track
    wide = tall * (1 + 0.26 * press) + rest * stretch
    cx = pad + inset + rest / 2 + (width - 2 * inset - rest) * at
    cy = pad + height / 2
    return (cx - wide / 2, cy - tall / 2, cx + wide / 2, cy + tall / 2)


def render(width: int, height: int, at: float, press: float = 0.0, hover: float = 0.0, stretch: float = 0.0,
           ground="#2a2925", pad: int = 4, on_color=TRACK_ON, off_color=TRACK_OFF) -> Image.Image:
    """The switch as an RGB picture of (width + 2 pad) x (height + 2 pad) on
    `ground`. at: 0 off .. 1 on (may overshoot a little while springing);
    press: 0 resting .. 1 held (glass); hover: 0..1; stretch: extra thumb
    length while it travels."""
    s = SUPERSAMPLE
    W, H, P = width * s, height * s, pad * s
    size = (W + 2 * P, H + 2 * P)
    base = Image.new("RGB", size, _rgb(ground))
    press = max(0.0, min(1.0, press))
    level = max(0.0, min(1.0, at))

    # Track: colour, groove shading, rim.
    track_box = (P, P, P + W, P + H)
    track = _capsule(size, track_box)
    color = _mix(_rgb(off_color), _rgb(on_color), level)
    color = _mix(color, (255, 255, 255), 0.07 * hover)
    _paint(base, color, track)
    top = ImageChops.subtract(track, _capsule(size, (P, P + H * 0.12, P + W, P + H * 1.12)))
    _paint(base, (0, 0, 0), _alpha(ImageChops.multiply(top.filter(ImageFilter.GaussianBlur(H * 0.16)), track), 0.55))
    bottom = ImageChops.subtract(track, _capsule(size, (P, P - H * 0.12, P + W, P + H * 0.88)))
    _paint(base, (255, 255, 255), _alpha(ImageChops.multiply(bottom.filter(ImageFilter.GaussianBlur(s)), track), 0.10))

    # Thumb geometry; the track as it is now is what the glass bends.
    box = tuple(value * s for value in thumb_box(width, height, pad, at, press, stretch))
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    thumb = _capsule(size, box)
    under = base.copy()

    # Drop shadow: tight at rest, wider and fainter when lifted.
    shadow = _capsule(size, (x0, y0 + h * 0.07, x1, y1 + h * 0.07))
    shadow = shadow.filter(ImageFilter.GaussianBlur(s * (1.3 + 2.2 * press)))
    _paint(base, (0, 0, 0), _alpha(shadow, 0.34 - 0.12 * press))

    # Refraction: a convex lens over the track (pressed only).
    if press > 0.01:
        rim = h * 0.16
        lens = _scaled(under, 0.94, cx, cy)                      # the rim pulls the outside in
        # Feathered zones, so the bend has no seams (the groove's shading
        # would otherwise show as lines across the glass).
        middle = _capsule(size, _shrink(box, rim * 0.5))
        lens.paste(_scaled(under, 1.10, cx, cy), (0, 0), middle.filter(ImageFilter.GaussianBlur(rim * 0.45)))
        inner = _capsule(size, _shrink(box, rim * 1.3))
        lens.paste(_scaled(under, 1.24, cx, cy), (0, 0), inner.filter(ImageFilter.GaussianBlur(rim * 0.6)))
        # Dispersion at the rim: red and blue part by a pixel or so.
        r, g, b = _scaled(under, 0.94, cx, cy).split()
        shift = max(1, round(s * 0.9 * press))
        fringe = Image.merge("RGB", (ImageChops.offset(r, shift, 0), g, ImageChops.offset(b, -shift, 0)))
        lens.paste(fringe, (0, 0), _alpha(ImageChops.subtract(thumb, middle), 0.65))
        lens = lens.filter(ImageFilter.GaussianBlur(s * 0.9))
        base.paste(lens, (0, 0), _alpha(thumb, press))

    # Body: white glass, near-opaque at rest, a faint veil when pressed;
    # a touch lighter at the top than at the bottom.
    top_alpha, bottom_alpha = 0.97 - 0.86 * press, 0.90 - 0.86 * press
    ramp = Image.linear_gradient("L").resize((size[0], max(1, round(h))), Image.BILINEAR)    # 0 top .. 255 bottom
    ramp = ramp.point(lambda value: round(255 * (top_alpha + (bottom_alpha - top_alpha) * value / 255)))
    body = Image.new("L", size, round(255 * top_alpha))
    body.paste(Image.new("L", size, round(255 * bottom_alpha)), (0, round(y1)))
    body.paste(ramp, (0, round(y0)))
    _paint(base, GLASS, ImageChops.multiply(body, thumb))

    # Thickness: a soft darker edge along the bottom inside the glass.
    lower = ImageChops.subtract(thumb, _capsule(size, (x0, y0 - h * 0.10, x1, y1 - h * 0.10)))
    _paint(base, (0, 0, 0), _alpha(ImageChops.multiply(lower.filter(ImageFilter.GaussianBlur(s * 1.2)), thumb),
                                   0.10 + 0.14 * press))

    # Fresnel rim: light where the glass curves away.
    edge = ImageChops.subtract(thumb, _capsule(size, _shrink(box, s * (1.1 + 0.6 * press))))
    _paint(base, (255, 255, 255), _alpha(edge.filter(ImageFilter.GaussianBlur(s * 0.5)), 0.38 + 0.42 * press))

    # Specular highlight: a thin arc of light along the upper-left edge (the
    # glass minus itself moved down and right), strongest at the top.
    arc = ImageChops.subtract(thumb, _capsule(size, (x0 + w * 0.05, y0 + h * 0.13, x1 + w * 0.05, y1 + h * 0.13)))
    fade = Image.linear_gradient("L").resize(size).point(lambda value: 255 - value)   # 255 at the top
    arc = ImageChops.multiply(arc.filter(ImageFilter.GaussianBlur(s * 0.7)), fade)
    _paint(base, (255, 255, 255), _alpha(ImageChops.multiply(arc, thumb), 0.30 + 0.60 * press))

    # Hairline so a resting thumb stays defined on a light track.
    outline = ImageChops.subtract(_capsule(size, _shrink(box, -s * 0.6)), thumb)
    _paint(base, (0, 0, 0), _alpha(outline, 0.16 * (1 - press)))

    return base.resize((width + 2 * pad, height + 2 * pad), Image.LANCZOS)
