"""Whether this computer is running on battery, for the battery saver: on
battery, idle sources are read less often, the icons are redrawn only when
something changed, and the local TPS records are scanned only when there is
new output (see refresh_scheduler and tray_app.tps_loop).

macOS asks IOKit for the time remaining on the power source, which reports
"unlimited" on mains power and on Macs without a battery. Windows asks
GetSystemPowerStatus. When the state cannot be read, the answer is mains
power: then nothing changes. CIRCLOW_POWER=battery or =ac overrides it."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading

log = logging.getLogger("Circlow")

CHECK_SECONDS = 20               # how often the power source is asked again
_MAC_UNLIMITED = -2.0            # kIOPSTimeRemainingUnlimited: on mains power
_iokit = None


def _mac_on_battery() -> bool | None:
    global _iokit
    if _iokit is None:
        _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        _iokit.IOPSGetTimeRemainingEstimate.restype = ctypes.c_double
        _iokit.IOPSGetTimeRemainingEstimate.argtypes = []
    return _iokit.IOPSGetTimeRemainingEstimate() != _MAC_UNLIMITED


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def _windows_on_battery() -> bool | None:
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    return {0: True, 1: False}.get(status.ACLineStatus)      # 255: unknown


def on_battery(platform: str | None = None, override: str | None = None) -> bool:
    """True while running on battery; False on mains power or when unknown."""
    override = (os.environ.get("CIRCLOW_POWER", "") if override is None else override).strip().lower()
    if override in ("battery", "ac"):
        return override == "battery"
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            return bool(_mac_on_battery())
        if platform == "win32":
            return bool(_windows_on_battery())
    except (OSError, AttributeError, ValueError):
        log.debug("power source could not be read", exc_info=True)
    return False


class PowerWatch:
    """The last answer of `read`, asked again every `every` seconds; logs
    each change. Safe to call from any thread."""

    def __init__(self, read=on_battery, every: float = CHECK_SECONDS):
        self.read, self.every = read, every
        self.lock = threading.Lock()
        self.state: bool | None = None
        self.checked = float("-inf")

    def battery(self, now: float) -> bool:
        with self.lock:
            if now - self.checked >= self.every or now < self.checked:
                self.checked = now
                try:
                    state = bool(self.read())
                except Exception:
                    log.debug("power source check failed", exc_info=True)
                    state = False
                if state != self.state:
                    log.info("power: %s", "on battery, battery saver on" if state else "on mains power, battery saver off")
                    self.state = state
            return bool(self.state)
