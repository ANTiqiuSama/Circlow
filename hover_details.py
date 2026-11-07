"""Hover UI for the AI tray icons: the agent panel, with the native tooltip
as a fallback when the layered panel cannot be created."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import itertools
import logging
import os
import sys
import time

import pystray
from pystray._util import win32

from detail_card import MARGIN
import agent_panel


WM_USER = 0x0400
TTM_ADDTOOLW = WM_USER + 50
TTM_UPDATETIPTEXTW = WM_USER + 57
TTM_TRACKACTIVATE = WM_USER + 17
TTM_TRACKPOSITION = WM_USER + 18
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
SWP_NOACTIVATE = 0x0010
SW_HIDE = 0
ULW_ALPHA = 2


class HoverIcon(pystray.Icon):
    """Keep an explicit icon ID and opt into application-owned hover UI."""

    # Each tray slot gets its own ID (set by the tray, 0..4): Windows keys an
    # icon's visibility and place on (program, ID), so shared IDs let it hide
    # or move one of them at random. Slot 0 keeps the old installation's 0.
    icon_id = 0
    details_visible = False

    def _message(self, code, flags, **kwargs):
        # pystray 0.19.5 passes hID instead of the actual uID struct member.
        # One icon per HWND lets us use a stable, documented identifier.
        return win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd,
            uID=self.icon_id, uFlags=flags, **kwargs))

    def _show(self):
        self.hover_open = False
        super()._show()
        # Version 4 suppresses the Shell's short tooltip without NIF_SHOWTIP.
        # Set this again after Explorer recreates the notification area.
        self._version4 = bool(self._message(4, 0, uVersion=4))

    icon_failed = False          # the Shell refused the last picture: send it again

    def _update_icon(self):
        # pystray ignores NIM_MODIFY's result. While Explorer is busy (it
        # reflows the tray when slots are reassigned) the call can fail, and
        # the slot keeps showing an old picture, even another source's,
        # until the image changes again; the tray retries on icon_failed.
        self._release_icon()
        self._assert_icon_handle()
        self.icon_failed = not self._message(win32.NIM_MODIFY, win32.NIF_ICON, hIcon=self._icon_handle)
        self._icon_valid = True
        if self.icon_failed:
            logging.getLogger(__name__).info("tray icon %d: the Shell did not take the new picture; will resend", self.icon_id)

    def resend_icon(self) -> bool:
        """Send the current picture again; True when the Shell took it."""
        if not self.visible or not self._hwnd or self.icon is None:
            return False
        self._update_icon()
        return not self.icon_failed

    def ensure_registered(self) -> bool:
        """Re-add an icon the Shell does not hold. pystray ignores a failed
        NIM_ADD (e.g. while Explorer is busy) and only retries on
        TaskbarCreated, so such an icon would stay missing until restart."""
        if not self.visible or not self._hwnd or self._message(win32.NIM_MODIFY, 0):
            return False
        self._show()
        return True

    def _on_notify(self, wparam, lparam):
        event = lparam & 0xFFFF if getattr(self, "_version4", False) else lparam
        action = getattr(self, "settings_action", None)
        if action and event in (0x007B, win32.WM_RBUTTONUP):
            self.hover_open = False
            self.suppress_hover = True
            action()
            return
        if not getattr(self, "_version4", False):
            return super()._on_notify(wparam, lparam)
        # In v4 the upper word is the icon ID; only the lower word is an event.
        event = lparam & 0xFFFF
        if event in (WM_USER, WM_USER + 1):  # NIN_SELECT / NIN_KEYSELECT
            self.hover_open = False
            self()
        elif event == 0x007B:  # WM_CONTEXTMENU
            self.hover_open = False
            super()._on_notify(0, win32.WM_RBUTTONUP)
        elif event in (WM_USER + 6, WM_USER + 7):  # POPUPOPEN / POPUPCLOSE
            self.hover_open = event == WM_USER + 6


def tray_visibility(executable: str | None = None) -> dict[int, bool]:
    """{icon ID: shown on the taskbar} as Windows 11 remembers it for this
    program (IsPromoted=1 on the taskbar, otherwise in the overflow flyout).
    Read only. Empty where unknown: Windows 10, or a source run, whose
    python.exe entries may belong to other programs."""
    if executable is None:
        if not getattr(sys, "frozen", False):
            return {}
        executable = sys.executable
    wanted = os.path.normcase(os.path.abspath(executable))
    result: dict[int, bool] = {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as root:
            for index in itertools.count():
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, name) as key:
                        path = str(winreg.QueryValueEx(key, "ExecutablePath")[0])
                        if os.path.normcase(os.path.abspath(path)) != wanted:
                            continue
                        uid = int(winreg.QueryValueEx(key, "UID")[0])
                        try:
                            promoted = int(winreg.QueryValueEx(key, "IsPromoted")[0]) == 1
                        except OSError:
                            promoted = False          # never promoted: Windows keeps it in the overflow
                        result[uid] = promoted
                except (OSError, TypeError, ValueError):
                    continue
    except (ImportError, OSError):
        return {}
    return result


class NotifyIconIdentifier(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]


class ToolInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("hwnd", wintypes.HWND), ("uId", ctypes.c_size_t),
                ("rect", wintypes.RECT), ("hinst", wintypes.HINSTANCE),
                ("lpszText", wintypes.LPWSTR), ("lParam", wintypes.LPARAM),
                ("lpReserved", ctypes.c_void_p)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32", use_last_error=True)
shell = ctypes.WinDLL("shell32", use_last_error=True)
common = ctypes.WinDLL("comctl32", use_last_error=True)
user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user.CreateWindowExW.restype = wintypes.HWND
user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user.SendMessageW.restype = ctypes.c_ssize_t
user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user.SetWindowPos.restype = wintypes.BOOL
user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user.DestroyWindow.argtypes = [wintypes.HWND]
user.WindowFromPoint.argtypes = [wintypes.POINT]
user.WindowFromPoint.restype = wintypes.HWND
user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user.GetAncestor.restype = wintypes.HWND
user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user.GetWindow.restype = wintypes.HWND
user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user.GetWindowLongW.restype = wintypes.LONG
user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user.GetAsyncKeyState.argtypes = [ctypes.c_int]
user.GetAsyncKeyState.restype = ctypes.c_short
user.IsWindowVisible.argtypes = [wintypes.HWND]
user.IsWindowVisible.restype = wintypes.BOOL
user.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
user.MonitorFromRect.restype = wintypes.HANDLE
user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
    wintypes.UINT, wintypes.UINT, wintypes.UINT]
user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.restype = ctypes.c_ssize_t
user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BlendFunction), wintypes.DWORD]
user.UpdateLayeredWindow.restype = wintypes.BOOL
gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi.CreateCompatibleDC.restype = wintypes.HDC
gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi.CreateDIBSection.restype = wintypes.HBITMAP
gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi.SelectObject.restype = wintypes.HGDIOBJ
gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi.DeleteDC.argtypes = [wintypes.HDC]
shell.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NotifyIconIdentifier), ctypes.POINTER(wintypes.RECT)]
shell.Shell_NotifyIconGetRect.restype = ctypes.c_long


def icon_rect(icon) -> tuple[int, int, int, int] | None:
    if not icon.visible or not icon._hwnd:
        return None
    identifier = NotifyIconIdentifier(cbSize=ctypes.sizeof(NotifyIconIdentifier),
        hWnd=icon._hwnd, uID=icon.icon_id)
    rect = wintypes.RECT()
    if shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect)) == 0:
        if rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    return None


def contains(rect, x, y) -> bool:
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


def tooltip_position(anchor, size, work_area, gap=8):
    """Keep the entire popup on the icon's monitor, including negative coords."""
    width, height = size
    left, top, right, bottom = work_area
    x = max(left, min(anchor[2] - width, right - width))
    y = anchor[1] - height - gap
    if y < top:
        y = anchor[3] + gap
    return x, max(top, min(y, bottom - height))


def notification_area_at(point) -> bool:
    # Shell keeps rectangles for hidden/occluded icons too. Do not display a
    # phantom popup when a fullscreen app covers the actual notification area.
    hwnd = user.WindowFromPoint(point)
    root = user.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT
    name = ctypes.create_unicode_buffer(256)
    if root:
        user.GetClassNameW(root, name, len(name))
    return name.value in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}


def is_window_covered(hwnd, rect) -> bool:
    # WindowFromPoint skips static text controls and transparent surfaces, so
    # hit testing cannot prove that a fullscreen window is behind our popup.
    above = user.GetWindow(hwnd, 3)  # GW_HWNDPREV, actual z-order
    for _ in range(256):
        if not above:
            return False
        if user.IsWindowVisible(above) and not (user.GetWindowLongW(above, -20) & 0x20):
            other = wintypes.RECT()
            if user.GetWindowRect(above, ctypes.byref(other)):
                if (other.left < rect.right and other.right > rect.left
                        and other.top < rect.bottom and other.bottom > rect.top):
                    return True
        above = user.GetWindow(above, 3)
    return False


def enable_thread_dpi_awareness() -> bool:
    """Per-monitor v2 for the hover thread only, so the card is drawn at the
    monitor's real DPI instead of being bitmap-stretched (blurry) by DWM. All
    coordinates this thread reads (cursor, icon rect, monitor) become physical
    together. CODEX_QUOTA_LEGACY_DPI=1 keeps the previous behaviour."""
    if os.environ.get("CODEX_QUOTA_LEGACY_DPI") == "1":
        return False
    try:
        function = user.SetThreadDpiAwarenessContext
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_void_p
        return bool(function(ctypes.c_void_p(-4)))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        return False


def monitor_scale(monitor) -> float:
    try:
        shcore = ctypes.WinDLL("shcore")
        x, y = wintypes.UINT(), wintypes.UINT()
        if shcore.GetDpiForMonitor(wintypes.HANDLE(monitor), 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return x.value / 96
    except (AttributeError, OSError):
        pass
    return 1.0


def content_text(content) -> str:
    return content if isinstance(content, str) else content.as_text()


class TrackingTooltip:
    """All window operations run on the owning thread; focus is never requested."""

    def __init__(self):
        common.InitCommonControls()
        self.owner = user.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC", "CodexQuotaTray hover controller", 0x80000000,
            0, 0, 0, 0, None, None, None, None)
        self.hwnd = user.CreateWindowExW(WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            "tooltips_class32", "AI 数据详情", 0x80000000 | 1 | 2 | 0x10 | 0x20,
            0, 0, 0, 0, self.owner, None, None, None)
        if not self.owner or not self.hwnd:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.tool = ToolInfo(cbSize=ctypes.sizeof(ToolInfo), uFlags=0x20 | 0x80,
            hwnd=self.owner, uId=1, lpszText="")  # TRACK | ABSOLUTE
        self.text = ""
        self.visible = False
        if not self.send(TTM_ADDTOOLW, 0, ctypes.addressof(self.tool)):
            self.close()
            raise OSError("无法创建 AI 悬停详情")
        self.send(WM_USER + 24, 0, 520)  # TTM_SETMAXTIPWIDTH enables multiline text

    def send(self, message, wparam=0, lparam=0):
        return user.SendMessageW(self.hwnd, message, wparam, lparam)

    def show(self, text, anchor):
        text = content_text(text)
        first_show = not self.visible
        if text != self.text:
            self.buffer = ctypes.create_unicode_buffer(text)
            self.tool.lpszText = ctypes.cast(self.buffer, wintypes.LPWSTR)
            self.send(TTM_UPDATETIPTEXTW, 0, ctypes.addressof(self.tool))
            self.text = text
        if not self.visible:
            position = (anchor[0] & 0xFFFF) | ((anchor[1] & 0xFFFF) << 16)
            self.send(TTM_TRACKPOSITION, 0, position)
            self.send(TTM_TRACKACTIVATE, 1, ctypes.addressof(self.tool))
            self.visible = True
        rect = wintypes.RECT()
        user.GetWindowRect(self.hwnd, ctypes.byref(rect))
        native_anchor = wintypes.RECT(*anchor)
        monitor = user.MonitorFromRect(ctypes.byref(native_anchor), 2)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        work = info.rcWork
        x, y = tooltip_position(anchor, (rect.right - rect.left, rect.bottom - rect.top),
            (work.left, work.top, work.right, work.bottom))
        if (rect.left, rect.top) != (x, y):
            # Keep the tooltip control's tracking position in sync as well.
            # Merely moving its HWND lets its next timer/text update snap back.
            self.send(TTM_TRACKPOSITION, 0, (x & 0xFFFF) | ((y & 0xFFFF) << 16))
        covered = is_window_covered(self.hwnd, rect)
        if first_show or covered or not user.IsWindowVisible(self.hwnd):
            # Show once; reassert only after actual occlusion. Repeating
            # SWP_SHOWWINDOW every 100ms restarts the tooltip's own lifecycle.
            if not user.SetWindowPos(self.hwnd, wintypes.HWND(-1), x, y, 0, 0,
                    0x0001 | SWP_NOACTIVATE | 0x0040):
                raise ctypes.WinError(ctypes.get_last_error())

    def hide(self):
        if self.visible:
            self.send(TTM_TRACKACTIVATE, 0, ctypes.addressof(self.tool))
            self.visible = False

    @staticmethod
    def pump():
        msg = wintypes.MSG()
        while user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user.TranslateMessage(ctypes.byref(msg))
            user.DispatchMessageW(ctypes.byref(msg))

    def close(self):
        for name in ("hwnd", "owner"):
            handle = getattr(self, name, None)
            if handle:
                user.DestroyWindow(handle)
                setattr(self, name, None)


def work_area(anchor):
    """(monitor, work rectangle) of the monitor holding the tray icon."""
    native_anchor = wintypes.RECT(*anchor)
    monitor = user.MonitorFromRect(ctypes.byref(native_anchor), 2)
    info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
    if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    work = info.rcWork
    return monitor, (work.left, work.top, work.right, work.bottom)


class LayeredSurface:
    """One memory DC + DIB reused for every frame of a layered window. Making a
    fresh screen DC, memory DC and DIB section 60 times a second churned GDI
    handles (a WinError 8 under memory pressure once disabled the bubbles).
    The DIB only grows; smaller frames use its top rows."""

    def __init__(self):
        self.memory = gdi.CreateCompatibleDC(None)
        if not self.memory:
            raise ctypes.WinError(ctypes.get_last_error())
        self.bitmap = self.previous = None
        self.bits = ctypes.c_void_p()
        self.width = self.height = 0

    def _reserve(self, width, height):
        if width == self.width and height <= self.height:
            return
        height = max(height, self.height if width == self.width else 0)
        header = BitmapInfoHeader(biSize=ctypes.sizeof(BitmapInfoHeader), biWidth=width,
            biHeight=-height, biPlanes=1, biBitCount=32, biCompression=0)
        bits = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(self.memory, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            raise ctypes.WinError(ctypes.get_last_error())
        old = gdi.SelectObject(self.memory, bitmap)
        if self.bitmap:
            gdi.DeleteObject(self.bitmap)
        else:
            self.previous = old
        self.bitmap, self.bits, self.width, self.height = bitmap, bits, width, height

    def present(self, hwnd, image, x, y, alpha=255):
        """Hand a straight-alpha PIL image to the window (premultiplied BGRA)."""
        width, height = image.size
        self._reserve(width, height)
        data = image.convert("RGBa").tobytes("raw", "BGRa")
        ctypes.memmove(self.bits, data, len(data))
        self._update(hwnd, x, y, alpha, (width, height))

    def move(self, hwnd, x, y, alpha=255):
        """Same pixels, new position or opacity (opening fade, closing)."""
        self._update(hwnd, x, y, alpha, None)

    def _update(self, hwnd, x, y, alpha, size):
        blend = BlendFunction(0, 0, max(0, min(255, alpha)), 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        ok = user.UpdateLayeredWindow(hwnd, None, ctypes.byref(wintypes.POINT(x, y)),
            ctypes.byref(wintypes.SIZE(*size)) if size else None, self.memory if size else None,
            ctypes.byref(wintypes.POINT(0, 0)) if size else None, 0, ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.memory:
            if self.previous:
                gdi.SelectObject(self.memory, self.previous)
            if self.bitmap:
                gdi.DeleteObject(self.bitmap)
            gdi.DeleteDC(self.memory)
        self.memory = self.bitmap = self.previous = None


class AgentPanelWindow:
    """Interactive bubble panel. Layered and topmost like the card, but it
    takes the pointer (no WS_EX_TRANSPARENT) so bubbles can be dragged; it is
    still never activated, so focus stays in the user's window.

    The scene is stepped on every tick (input), but a frame is only drawn when
    something moves (every tick), for slow ambient loops (30 fps), or after a
    data change; a panel at rest costs no rendering at all."""

    OPEN_SECONDS, CLOSE_SECONDS = 0.2, 0.14
    AMBIENT_SECONDS = 1 / 30 - 0.004       # every second 60 Hz tick, despite timer jitter

    def __init__(self):
        self.hwnd = user.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            "STATIC", "AI Agent 任务", 0x80000000 | 0x0100, 0, 0, 0, 0, None, None, None, None)  # WS_POPUP | SS_NOTIFY
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.surface = LayeredSurface()
        except Exception:
            user.DestroyWindow(self.hwnd)
            raise
        self.scene = agent_panel.Scene()
        self.provider = None
        self.anchor = None
        self.rect = None
        self.scale = 1.0
        self.work = None
        self.opened_at = self.closing_at = None
        self.shown = False
        self.checked_at = 0.0
        self.image = None           # last presented frame
        self.placed = None          # (x, y, alpha) it was presented at
        self.drawn_at = 0.0

    @property
    def active(self) -> bool:
        return self.provider is not None

    def open(self, provider, model, anchor, now):
        monitor, self.work = work_area(anchor)
        scale = monitor_scale(monitor)
        if self.provider is None or self.closing_at is not None:
            self.scene = agent_panel.Scene()
            self.opened_at, self.closing_at = now, None
            self.image = None
        if scale != self.scale or anchor != self.anchor:
            self.image = None
        self.scale = scale
        self.provider, self.anchor = provider, anchor
        self.scene.set_model(model, now)

    def refresh(self, model, now):
        self.scene.set_model(model, now)

    def contains(self, point, slack=6) -> bool:
        rect = self.rect
        return bool(rect) and rect[0] - slack <= point.x < rect[2] + slack and rect[1] - slack <= point.y < rect[3] + slack

    def local(self, point):
        if not self.rect:
            return None
        origin = MARGIN * self.scale
        return ((point.x - self.rect[0] - origin) / self.scale, (point.y - self.rect[1] - origin) / self.scale)

    def close(self, now):
        if self.provider is not None and self.closing_at is None:
            self.closing_at = now

    def frame(self, point, left, right, now):
        """Advance the scene and present a frame if one is due; returns input events."""
        inside = point is not None and self.contains(point, 0)
        scene = self.scene
        events = scene.step(now, self.local(point) if inside or scene.dragging else None,
                            left and (inside or bool(scene.press)), right and inside)
        if self.closing_at is not None:
            progress = min(1.0, (now - self.closing_at) / self.CLOSE_SECONDS)
            if progress >= 1:
                self.hide()
                return []
            alpha, lift = 1 - progress, -4 * progress
        else:
            progress = agent_panel.ease_out_cubic((now - self.opened_at) / self.OPEN_SECONDS)
            alpha, lift = progress, 10 * (1 - progress)
        if (self.image is None or scene.dirty or scene.animating
                or scene.ambient and now - self.drawn_at >= self.AMBIENT_SECONDS):
            image, self.drawn_at = scene.render(self.scale), now
        else:
            image = None
        size = (image or self.image).size
        margin = round(MARGIN * self.scale)
        x, y = tooltip_position(self.anchor, size, self.work, gap=round(6 * self.scale) - margin)
        placed = (x, y + round(lift * self.scale), round(255 * alpha))
        if image is not None:
            self.surface.present(self.hwnd, image, *placed)
            self.image = image
        elif placed != self.placed:
            self.surface.move(self.hwnd, *placed)
        self.placed = placed
        self.rect = (placed[0], placed[1], placed[0] + size[0], placed[1] + size[1])
        if not self.shown or now - self.checked_at > 0.5:
            # Z-order walk is cheap but not free; twice a second is plenty.
            self.checked_at = now
            if not self.shown or is_window_covered(self.hwnd, wintypes.RECT(*self.rect)):
                if not user.SetWindowPos(self.hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOACTIVATE | 0x0040):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not self.shown:
                    logging.getLogger(__name__).info('agent panel shown: provider=%s bubbles=%s rect=%s',
                                                     self.provider, len(scene.bodies), self.rect)
                self.shown = True
        return events

    def hide(self):
        user.ShowWindow(self.hwnd, SW_HIDE)
        self.provider = self.rect = self.closing_at = None
        self.image = self.placed = None
        self.shown = False

    def destroy(self):
        if self.hwnd:
            user.DestroyWindow(self.hwnd)
            self.hwnd = None
        if getattr(self, "surface", None):
            self.surface.close()
            self.surface = None


class HoverDetails:
    """Hover a tray icon for its agent panel; it stays while the pointer is on
    it. Without a panel (no `agents`, or a GDI failure) the native tooltip
    shows `text_for(provider)`."""

    PANEL_GRACE = 0.45          # Time to travel from the icon to the panel.
    PANEL_REFRESH = 0.5
    PANEL_RETRY_SECONDS = 2.0

    def __init__(self, icons, text_for, stop_event, agents=None):
        self.icons, self.text_for, self.stop_event = icons, text_for, stop_event
        self.agents = agents     # object with panel(provider), merge(a, b), split(key)
        self.candidate = None
        self.entered_at = 0.0
        self.panel_seen = 0.0
        self.panel_built = 0.0

    def update(self, popup, provider, anchor, now, dwell=0.4):
        if provider != self.candidate:
            popup.hide()
            self.candidate, self.entered_at = provider, now
        if provider is None:
            popup.hide()
        elif now - self.entered_at >= dwell:
            popup.show(self.text_for(provider), anchor)

    def target_at(self, point):
        for key, icon in self.icons.items():
            rect = icon_rect(icon)
            if getattr(icon, "suppress_hover", False):
                if not rect or not contains(rect, point.x, point.y):
                    icon.suppress_hover = False
                continue
            # A Shell refresh can send POPUPCLOSE while the mouse is still on
            # the icon. Once opened, keep our popup until the pointer leaves.
            if (getattr(icon, "_version4", False) and not icon.hover_open
                    and self.candidate != key):
                continue
            if rect and contains(rect, point.x, point.y):
                return key, rect
        return None, None

    def drive_panel(self, panel, point, provider, anchor, left, right, now) -> None:
        """One frame of the open panel: hand input to the scene, act on drops."""
        if provider:
            # Remember the icon actually under the pointer. After switching icons
            # the Shell sends POPUPCLOSE a few seconds later; with the old
            # candidate the new icon stopped counting as hovered and the panel
            # closed while the pointer was still on it.
            self.candidate = provider
        if provider and provider != panel.provider and not panel.scene.dragging:
            model = self.agents.panel(provider)
            if model is None:           # e.g. Claude without a subscription: tooltip only
                panel.close(now)
                return
            panel.open(provider, model, anchor, now)
        over = panel.contains(point) or provider == panel.provider
        if over or panel.scene.dragging:
            self.panel_seen = now
        elif now - self.panel_seen > self.PANEL_GRACE or (left or right) and not panel.contains(point, 40):
            panel.close(now)
        if (now - self.panel_built >= self.PANEL_REFRESH and panel.closing_at is None
                and not panel.scene.press and not panel.scene.dragging):
            model = self.agents.panel(panel.provider)
            if model is None:
                panel.close(now)
                return
            panel.refresh(model, now)
            self.panel_built = now
        for event in panel.frame(point, left, right, now):
            if event[0] == "settings":
                panel.hide()
                self.candidate = None
                if len(event) > 1 and hasattr(self.agents, "open_settings_page"):
                    self.agents.open_settings_page(event[1])
                else:
                    self.agents.open_settings()
                return
            elif event[0] == "merge":
                merged = self.agents.merge(event[1], event[2])
                if merged:
                    panel.scene.absorb(event[1], event[2], merged, now)
            elif event[0] == "split":
                members = self.agents.split(event[1])
                if members:
                    panel.scene.burst(event[1], members, now)
            model = self.agents.panel(panel.provider)
            if model is not None:
                panel.refresh(model, now)

    FRAME_SECONDS = 1 / 60

    def pause(self, delay: float) -> bool:
        """Sleep until the next tick; True once the tray is stopping. Event.wait
        follows the 15.6 ms system tick, which turns a 16 ms frame into an
        uneven 15/31 ms cadence (visible judder); time.sleep is high resolution."""
        if delay < 0.05:
            time.sleep(delay)
            return self.stop_event.is_set()
        return self.stop_event.wait(delay)

    POINTER_REUSE = 1.0          # a still pointer keeps its last answer this long

    def pointer(self):
        """(point, provider, anchor, left, right) for this tick. While the
        pointer and buttons have not moved, the previous answer is reused for
        up to POINTER_REUSE seconds: ten window lookups a second for a mouse
        at rest were most of this thread's CPU."""
        point = wintypes.POINT()
        have_point = bool(user.GetCursorPos(ctypes.byref(point)))
        left, right = (bool(user.GetAsyncKeyState(key) & 0x8000) for key in (1, 2))
        key = (have_point, point.x, point.y, left, right)
        now = time.monotonic()
        held = getattr(self, "_pointer_held", None)
        if held is not None and held[0] == key and now - held[1] < self.POINTER_REUSE:
            return held[2]
        provider = anchor = None
        if have_point and notification_area_at(point) and not (left or right):
            provider, anchor = self.target_at(point)
        elif have_point:
            for icon in self.icons.values():     # target_at clears this itself on the tray
                if getattr(icon, "suppress_hover", False):
                    rect = icon_rect(icon)
                    if not rect or not contains(rect, point.x, point.y):
                        icon.suppress_hover = False
        result = (point, provider, anchor, left, right)
        self._pointer_held = (key, now, result)
        return result

    def dwell(self, provider) -> float:
        # Shell v4 already waited for its own hover delay before POPUPOPEN.
        return 0.0 if provider and getattr(self.icons[provider], "_version4", False) else 0.4

    def run(self):
        log = logging.getLogger(__name__)
        tooltip = panel = None
        retry_at = 0.0
        try:
            enable_thread_dpi_awareness()
            if self.agents is not None:
                try:
                    panel = AgentPanelWindow()
                except Exception:
                    log.exception("agent panel unavailable, using tooltip")
                    retry_at = time.monotonic() + self.PANEL_RETRY_SECONDS
            if panel is None:
                tooltip = TrackingTooltip()
            deadline = time.monotonic()
            while True:
                period = self.FRAME_SECONDS if panel is not None and panel.active else 0.1
                deadline = max(deadline + period, time.monotonic())
                if self.pause(max(0.0, deadline - time.monotonic())):
                    break
                TrackingTooltip.pump()
                requested = getattr(self.agents, "settings_requested", None)
                if requested is not None and requested.is_set():
                    requested.clear()
                    self.candidate = None
                    for popup in (panel, tooltip):
                        if popup:
                            popup.hide()
                point, provider, anchor, left, right = self.pointer()
                now = time.monotonic()
                if panel is None and self.agents is not None and provider and now >= retry_at:
                    try:
                        panel = AgentPanelWindow()
                        log.info("agent panel recreated after temporary failure")
                    except Exception:
                        log.exception("agent panel retry failed, using tooltip")
                        retry_at = now + self.PANEL_RETRY_SECONDS
                if panel is None:
                    for key, icon in self.icons.items():
                        icon.details_visible = key == provider
                    tooltip = tooltip or TrackingTooltip()
                    self.update(tooltip, provider, anchor, now, dwell=self.dwell(provider))
                    continue
                try:
                    if panel.active:
                        self.drive_panel(panel, point, provider, anchor, left, right, now)
                    else:
                        if provider != self.candidate:
                            self.candidate, self.entered_at = provider, now
                        due = provider and now - self.entered_at >= self.dwell(provider)
                        model = self.agents.panel(provider) if due else None
                        if model is not None:
                            panel.open(provider, model, anchor, now)
                            self.panel_seen = self.panel_built = now
                        elif due:
                            # Sources without a quota panel keep the native tooltip.
                            tooltip = tooltip or TrackingTooltip()
                            tooltip.show(self.text_for(provider), anchor)
                    if tooltip and (panel.active or provider is None):
                        tooltip.hide()
                    shown = panel.provider if panel.active else (provider if tooltip and tooltip.visible else None)
                except Exception:
                    # A transient GDI failure must not disable bubbles for the
                    # rest of this process. Retry on hover, at a bounded rate.
                    log.exception("agent panel failed, using tooltip")
                    panel.hide()
                    panel.destroy()
                    panel = None
                    tooltip = tooltip or TrackingTooltip()
                    retry_at = now + self.PANEL_RETRY_SECONDS
                    shown = None
                for key, icon in self.icons.items():
                    icon.details_visible = key == shown
        except Exception:
            log.exception("AI hover details stopped")
            # Restore the Shell tooltip if the independent popup cannot run.
            for icon in self.icons.values():
                icon._message(1, win32.NIF_TIP | 0x80, szTip=icon.title)
        finally:
            for icon in self.icons.values():
                icon.details_visible = False
            if tooltip:
                tooltip.close()
            if panel:
                panel.destroy()
