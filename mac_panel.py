"""macOS hover panel: the Windows agent panel (agent_panel.Scene) shown in a
borderless, non-activating NSPanel under its menu bar item.

Same model as hover_details.HoverDetails: a timer on the AppKit main thread
polls the pointer and mouse buttons, opens the panel after a short dwell on a
status item, steps the scene with that input and presents a frame only when
something moved. The panel never takes focus; it closes once the pointer has
left both the item and the panel for PANEL_GRACE seconds.

Geometry is kept in screen points with Cocoa's bottom-left origin; the scene
works in panel points with a top-left origin (its logical units), so the
conversions below only flip y and subtract the shadow margin.
"""
from __future__ import annotations

import logging
import time

import agent_panel
from detail_card import MARGIN

log = logging.getLogger("Circlow")

DWELL = 0.4                 # pointer rest on an item before its panel opens
GAP = 6                     # menu bar bottom to the card's top edge, in points


def contains(frame, point, slack: float = 0.0) -> bool:
    """frame: ((x, y), (w, h)) in screen points; point: (x, y)."""
    (x, y), (w, h) = frame
    return x - slack <= point[0] < x + w + slack and y - slack <= point[1] < y + h + slack


def panel_origin(anchor, size, visible) -> tuple[float, float]:
    """Bottom-left of a panel of `size` (points, shadow margin included)
    hanging under the status item `anchor`, centred on it and kept inside the
    screen's visible frame. The transparent margin overlaps the gap."""
    (ax, ay), (aw, _ah) = anchor
    (vx, vy), (vw, _vh) = visible
    width, height = size
    x = ax + aw / 2 - width / 2
    x = max(vx, min(x, vx + vw - width))
    top = ay - GAP + MARGIN
    return x, max(vy, top - height)


def local_point(frame, point) -> tuple[float, float]:
    """Screen point -> scene coordinates (top-left origin, inside the margin)."""
    (x, y), (_w, h) = frame
    return point[0] - x - MARGIN, y + h - point[1] - MARGIN


def to_nsimage(image, scale: float):
    """PIL RGBA -> NSImage at `scale` pixels per point (about 1 ms at 2x)."""
    from AppKit import NSBitmapImageRep, NSDeviceRGBColorSpace, NSImage
    width, height = image.size
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bitmapFormat_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, NSDeviceRGBColorSpace,
        2,                      # NSBitmapFormatAlphaNonpremultiplied: PIL's own layout
        width * 4, 32)
    memoryview(rep.bitmapData())[:] = image.convert("RGBA").tobytes()
    native = NSImage.alloc().initWithSize_((width / scale, height / scale))
    native.addRepresentation_(rep)
    return native


GLASS_TINT = (22, 21, 19, 0.62)   # the card's own dark colour, so the light text reads on any wallpaper


def glass_available() -> bool:
    import os
    if os.environ.get("CIRCLOW_GLASS", "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    try:
        import objc
        objc.lookUpClass("NSGlassEffectView")
        return True
    except Exception:
        return False


def make_glass():
    """macOS 26 Liquid Glass for the panel card (NSGlassEffectView), tinted
    with the card colour and rounded like the drawn card; None where the
    system has no glass, and the panel draws its own card instead."""
    if not glass_available():
        return None
    import objc
    from AppKit import NSColor
    from detail_card import RADIUS
    glass = objc.lookUpClass("NSGlassEffectView").alloc().initWithFrame_(((MARGIN, MARGIN), (10, 10)))
    glass.setCornerRadius_(float(RADIUS))
    r, g, b, alpha = GLASS_TINT
    glass.setTintColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, alpha))
    return glass


_PANEL_CLASS = None


def panel_class():
    """NSPanel that keeps the frame it is given. AppKit otherwise pushes a
    window below the menu bar, and the panel's transparent shadow margin
    (which overlaps the bar by design) would move the card down 8 points."""
    global _PANEL_CLASS
    if _PANEL_CLASS is None:
        from AppKit import NSPanel

        class HoverPanelWindow(NSPanel):
            def constrainFrameRect_toScreen_(self, rect, screen):
                return rect

            def canBecomeKeyWindow(self):
                return False

        _PANEL_CLASS = HoverPanelWindow
    return _PANEL_CLASS


class PanelWindow:
    """One NSPanel reused for every source; the scene is rebuilt on open."""

    OPEN_SECONDS, CLOSE_SECONDS = 0.2, 0.14
    AMBIENT_SECONDS = 1 / 30 - 0.004

    def __init__(self):
        from AppKit import (NSBackingStoreBuffered, NSColor, NSImageView,
                            NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary,
                            NSWindowCollectionBehaviorIgnoresCycle,
                            NSWindowCollectionBehaviorStationary,
                            NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel)
        panel = panel_class().alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (10, 10)), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setLevel_(25)                      # NSStatusWindowLevel: above normal and floating windows
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setIgnoresMouseEvents_(False)      # clicks land here, not on the window below
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorStationary
                                     | NSWindowCollectionBehaviorIgnoresCycle
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary)
        view = NSImageView.alloc().initWithFrame_(((0, 0), (10, 10)))
        view.setImageScaling_(1)                 # NSImageScaleAxesIndependently: fill the frame exactly
        view.setEditable_(False)
        self.glass = make_glass()
        if self.glass is not None:
            # Liquid Glass card: the glass draws the card (and the window its
            # shadow); the image above it holds only the content.
            from AppKit import NSAppearance, NSView
            container = NSView.alloc().initWithFrame_(((0, 0), (10, 10)))
            container.addSubview_(self.glass)
            container.addSubview_(view)
            panel.setContentView_(container)
            panel.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            panel.setHasShadow_(True)
        else:
            panel.setContentView_(view)
            panel.setHasShadow_(False)           # the image carries its own soft shadow
        agent_panel.set_glass(self.glass is not None)
        self.panel, self.view = panel, view
        self.scene = agent_panel.Scene()
        self.provider = None
        self.anchor = None
        self.visible_frame = None
        self.scale = 2.0
        self.opened_at = self.closing_at = None
        self.frame = None            # ((x, y), (w, h)) as last placed
        self.size = None             # current image size in points
        self.drawn_at = 0.0
        self.has_image = False

    @property
    def active(self) -> bool:
        return self.provider is not None

    def open(self, provider, model, anchor, screen, now):
        if self.provider is None or self.closing_at is not None:
            self.scene = agent_panel.Scene()
            self.opened_at, self.closing_at = now, None
            self.has_image = False
        scale = float(screen.backingScaleFactor()) if screen is not None else 2.0
        if scale != self.scale or anchor != self.anchor:
            self.has_image = False
        self.scale = scale
        self.visible_frame = screen.visibleFrame() if screen is not None else anchor
        self.provider, self.anchor = provider, anchor
        self.scene.set_model(model, now)

    def refresh(self, model, now):
        self.scene.set_model(model, now)

    def contains(self, point, slack: float = 6) -> bool:
        return self.frame is not None and contains(self.frame, point, slack)

    def close(self, now):
        if self.provider is not None and self.closing_at is None:
            self.closing_at = now

    def step(self, point, left, right, now):
        """Advance the scene and present a frame if one is due; returns its events."""
        inside = point is not None and self.contains(point, 0)
        scene = self.scene
        local = local_point(self.frame, point) if self.frame is not None and (inside or scene.dragging) else None
        events = scene.step(now, local, left and (inside or bool(scene.press)), right and inside)
        if self.closing_at is not None:
            progress = min(1.0, (now - self.closing_at) / self.CLOSE_SECONDS)
            if progress >= 1:
                self.hide()
                return []
            alpha, lift = 1 - progress, -4 * progress
        else:
            progress = agent_panel.ease_out_cubic((now - self.opened_at) / self.OPEN_SECONDS)
            alpha, lift = progress, 10 * (1 - progress)
        if (not self.has_image or scene.dirty or scene.animating
                or scene.ambient and now - self.drawn_at >= self.AMBIENT_SECONDS):
            image = scene.render(self.scale)
            self.view.setImage_(to_nsimage(image, self.scale))
            self.size = (image.size[0] / self.scale, image.size[1] / self.scale)
            self.has_image, self.drawn_at = True, now
        x, y = panel_origin(self.anchor, self.size, self.visible_frame)
        frame = ((x, y - lift), self.size)
        if frame != self.frame:
            if self.glass is not None and (self.frame is None or self.frame[1] != self.size):
                width, height = self.size
                self.view.setFrame_(((0, 0), (width, height)))
                self.glass.setFrame_(((MARGIN, MARGIN), (max(1, width - 2 * MARGIN), max(1, height - 2 * MARGIN))))
            self.panel.setFrame_display_(frame, True)
            self.frame = frame
        self.panel.setAlphaValue_(alpha)
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()
            log.info("agent panel shown: provider=%s bubbles=%s frame=%s", self.provider, len(scene.bodies), frame)
        return events

    def hide(self):
        self.panel.orderOut_(None)
        self.provider = self.frame = self.closing_at = None
        self.has_image = False


class MacHoverPanel:
    """Drives PanelWindow from a main-thread timer.

    tray: QuotaTray (panel(), merge(), split()); items: source -> NSStatusItem;
    icons: source -> stand-in icon whose details_visible marks the hovered
    source for the refresh scheduler; on_settings(provider, page) handles the
    panel's settings gear and right-click."""

    PANEL_GRACE = 0.45
    PANEL_REFRESH = 0.5
    RETRY_SECONDS = 2.0
    FAST, SLOW = 1 / 60, 0.1

    def __init__(self, tray, items: dict, icons: dict, on_settings):
        self.tray, self.items, self.icons, self.on_settings = tray, items, icons, on_settings
        self.window: PanelWindow | None = None
        self.candidate = None
        self.entered_at = 0.0
        self.panel_seen = 0.0
        self.panel_built = 0.0
        self.retry_at = 0.0
        self.menu_open: str | None = None    # a status menu is tracking: no panel meanwhile
        self.suppressed: set[str] = set()    # clicked items stay quiet until the pointer leaves them
        self.timer = None
        self.period = None

    # ----- timer ----------------------------------------------------------------

    def start(self, target):
        """`target` is the NSObject whose `panelTick:` calls tick()."""
        self.target = target
        self._schedule(self.SLOW)

    def _schedule(self, period):
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
        if self.period == period:
            return
        if self.timer is not None:
            self.timer.invalidate()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            period, self.target, "panelTick:", None, True)
        # Common modes: keeps stepping while a menu or a drag runs its own loop.
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        self.period = period

    def stop(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        if self.window is not None:
            self.window.hide()

    # ----- menus ----------------------------------------------------------------

    def menu_will_open(self, provider: str):
        self.menu_open = provider
        if self.window is not None and self.window.active:
            self.window.hide()

    def menu_did_close(self, provider: str):
        self.menu_open = None
        self.suppressed.add(provider)
        self.candidate = None

    def clicked(self, provider: str):
        """A left-click opened the source: drop the panel until the pointer leaves the item."""
        if self.window is not None and self.window.active:
            self.window.hide()
        self.suppressed.add(provider)
        self.candidate = None

    # ----- input ----------------------------------------------------------------

    @staticmethod
    def pointer():
        from AppKit import NSEvent
        location = NSEvent.mouseLocation()
        buttons = int(NSEvent.pressedMouseButtons())
        return (float(location.x), float(location.y)), bool(buttons & 1), bool(buttons & 2)

    def target_at(self, point):
        """(source, anchor frame, screen) of the status item under the pointer."""
        for key, item in self.items.items():
            button = item.button()
            window = button.window() if button is not None else None
            if window is None:
                continue
            frame = window.frame()
            anchor = ((float(frame.origin.x), float(frame.origin.y)), (float(frame.size.width), float(frame.size.height)))
            if contains(anchor, point):
                if key in self.suppressed:
                    return None, None, None
                return key, anchor, window.screen()
        return None, None, None

    def release_suppressed(self, point):
        for key in list(self.suppressed):
            window = self.items[key].button().window() if key in self.items else None
            if window is None:
                self.suppressed.discard(key)
                continue
            frame = window.frame()
            anchor = ((frame.origin.x, frame.origin.y), (frame.size.width, frame.size.height))
            if not contains(anchor, point):
                self.suppressed.discard(key)

    # ----- one tick -------------------------------------------------------------

    def tick(self):
        now = time.monotonic()
        point, left, right = self.pointer()
        self.release_suppressed(point)
        shown = None
        try:
            if self.menu_open is not None:
                self.candidate = None
            else:
                provider, anchor, screen = self.target_at(point) if not (left or right) else (None, None, None)
                if self.window is None and provider and now >= self.retry_at:
                    self.window = PanelWindow()
                window = self.window
                if window is not None and window.active:
                    self.drive(window, point, provider, anchor, screen, left, right, now)
                elif window is not None:
                    if provider != self.candidate:
                        self.candidate, self.entered_at = provider, now
                    if provider and now - self.entered_at >= DWELL:
                        model = self.tray.panel(provider)
                        if model is not None:
                            window.open(provider, model, anchor, screen, now)
                            self.panel_seen = self.panel_built = now
                            window.step(point, left, right, now)
                shown = window.provider if window is not None and window.active else None
        except Exception:
            # A failed frame must not end hover panels for the rest of the run.
            log.exception("mac agent panel failed")
            if self.window is not None:
                try:
                    self.window.hide()
                except Exception:
                    pass
            self.window = None
            self.retry_at = now + self.RETRY_SECONDS
            shown = None
        for key, icon in self.icons.items():
            icon.details_visible = key == shown
        self._schedule(self.FAST if shown else self.SLOW)

    def drive(self, window: PanelWindow, point, provider, anchor, screen, left, right, now):
        if provider:
            self.candidate = provider
        if provider and provider != window.provider and not window.scene.dragging:
            model = self.tray.panel(provider)
            if model is None:
                window.close(now)
                return
            window.open(provider, model, anchor, screen, now)
        over = window.contains(point) or provider == window.provider
        if over or window.scene.dragging:
            self.panel_seen = now
        elif now - self.panel_seen > self.PANEL_GRACE or (left or right) and not window.contains(point, 40):
            window.close(now)
        if (now - self.panel_built >= self.PANEL_REFRESH and window.closing_at is None
                and not window.scene.press and not window.scene.dragging):
            model = self.tray.panel(window.provider)
            if model is None:
                window.close(now)
                return
            window.refresh(model, now)
            self.panel_built = now
        for event in window.step(point, left, right, now):
            if event[0] == "settings":
                provider = window.provider
                window.hide()
                self.candidate = None
                if provider:
                    self.suppressed.add(provider)
                self.on_settings(provider, event[1] if len(event) > 1 else None)
                return
            if event[0] == "merge":
                merged = self.tray.merge(event[1], event[2])
                if merged:
                    window.scene.absorb(event[1], event[2], merged, now)
            elif event[0] == "split":
                members = self.tray.split(event[1])
                if members:
                    window.scene.burst(event[1], members, now)
            model = self.tray.panel(window.provider)
            if model is not None:
                window.refresh(model, now)
