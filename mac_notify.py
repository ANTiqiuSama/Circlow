"""macOS notifications from 环流 itself.

The menu bar used `osascript -e 'display notification …'`, which macOS files
under Script Editor: the banner carries Script Editor's name and icon, shows
only if Script Editor is allowed to show banners, and the app never learns
whether it was shown. A packaged Circlow.app posts through the
UserNotifications framework instead: it asks once for permission ("环流 wants
to send notifications"), its banners carry its own name and icon, it is
listed under its own name in System Settings → Notifications, and it knows
whether people said yes.

Without an app bundle (running from source) the framework is unavailable and
the old osascript banner is used. When people turned 环流's notifications
off, nothing is posted elsewhere instead.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid

log = logging.getLogger("Circlow")

UNKNOWN, GRANTED, DENIED, UNAVAILABLE = "unknown", "granted", "denied", "unavailable"


_DELEGATE_CLASS = None


def delegate_class(objc, NSObject, UN):
    """The Notification Center delegate, defined once per process under a name
    of its own: Objective-C class names are global, and mac_menu already has
    a `Delegate`, so a second class of that name would fail to register."""
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is None:
        present = UN.UNNotificationPresentationOptionBanner | UN.UNNotificationPresentationOptionList

        class CirclowNotificationDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
            # macOS keeps a banner quiet while its app is the active one; 环流
            # is a menu bar app whose notices matter exactly then too.
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, _center, _note, handler):
                handler(present)

        _DELEGATE_CLASS = CirclowNotificationDelegate
    return _DELEGATE_CLASS


def osascript_command(title: str, message: str) -> list[str]:
    def quoted(text: str) -> str:
        return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return ["osascript", "-e", f"display notification {quoted(message)} with title {quoted(title)}"]


class Notifier:
    """One per process. setup() once (the menu bar does it at launch), then
    post() from any thread."""

    def __init__(self, popen=subprocess.Popen):
        self.popen = popen
        self.state = UNKNOWN
        self.center = None
        self.delegate = None
        self.decided = threading.Event()

    # ----- permission -------------------------------------------------------------
    def setup(self, bundle_id: str | None = None) -> str:
        """`bundle_id` defaults to this process' own (none when run from source)."""
        try:
            if bundle_id is None:
                from Foundation import NSBundle
                bundle_id = NSBundle.mainBundle().bundleIdentifier()
            if not bundle_id:
                raise RuntimeError("no app bundle")
            import objc
            from Foundation import NSObject
            import UserNotifications as UN
        except Exception as error:
            log.info("native notifications unavailable (%s); using osascript", error)
            self.state = UNAVAILABLE
            self.decided.set()
            return self.state
        self.center = UN.UNUserNotificationCenter.currentNotificationCenter()
        self.delegate = delegate_class(objc, NSObject, UN).alloc().init()
        self.center.setDelegate_(self.delegate)
        self.center.requestAuthorizationWithOptions_completionHandler_(
            UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound, self._answered)
        return self.state

    def _answered(self, granted, error):
        self.state = GRANTED if granted else DENIED
        if error is not None:
            log.warning("notification permission: %s", error)
        log.info("notification permission: %s", self.state)
        self.decided.set()

    def wait_for_answer(self, seconds: float) -> str:
        self.decided.wait(seconds)
        return self.state

    # ----- posting ----------------------------------------------------------------
    def post(self, title: str, message: str, done=None) -> str:
        """Show a notification; returns how: "native" or "osascript".

        Natively it always goes to macOS, whatever answer this process
        remembers: a launch-time request can be answered with an error while
        the permission prompt is still up, and the person may change the
        setting later. macOS itself drops it when 环流's notifications are off
        (the reason is logged); it is never shown another way instead."""
        if self.center is None:
            self.popen(osascript_command(title or "环流", message), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "osascript"
        import UserNotifications as UN
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title or "环流")
        content.setBody_(message)
        content.setSound_(UN.UNNotificationSound.defaultSound())
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(f"circlow-{uuid.uuid4().hex}", content, None)

        def added(error):
            if error is not None:
                log.warning("notification %r was not added: %s", title, error)
            if done is not None:
                done(error)
        self.center.addNotificationRequest_withCompletionHandler_(request, added)
        return "native"

    def delivered_count(self, seconds: float = 5.0) -> int | None:
        """How many of this app's notifications Notification Center holds (self-test)."""
        if self.center is None:
            return None
        found, ready = [], threading.Event()

        def got(notes):
            found.append(len(notes or []))
            ready.set()
        self.center.getDeliveredNotificationsWithCompletionHandler_(got)
        ready.wait(seconds)
        return found[0] if found else None


NOTIFIER = Notifier()


def self_test(wait_seconds: float = 120.0) -> int:
    """`Circlow --notify-test`: ask for permission if needed, post one test
    banner and report what happened. Exit status 0 when it was delivered."""
    from Foundation import NSDate, NSRunLoop
    notifier = Notifier()
    notifier.setup()
    print("通知方式:", "原生（环流）" if notifier.center is not None else "osascript（脚本编辑器）", flush=True)
    if notifier.center is None:
        notifier.post("环流 · 测试通知", "这是一条测试通知。")
        return 0
    print("等待通知权限…（首次运行请在弹出的“环流”通知请求上点“允许”）", flush=True)
    deadline = time.monotonic() + wait_seconds
    while not notifier.decided.is_set() and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
    print("通知权限:", {GRANTED: "已允许", DENIED: "已关闭", UNKNOWN: "未回应"}.get(notifier.state, notifier.state), flush=True)
    if notifier.state != GRANTED:
        return 2
    before = notifier.delivered_count() or 0
    added = threading.Event()
    errors = []
    notifier.post("环流 · 测试通知", "通知工作正常：额度重置和全局重置预测会这样提醒你。", done=lambda error: (errors.append(error), added.set()))
    while not added.is_set() and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
    time.sleep(1.0)
    after = notifier.delivered_count() or 0
    ok = added.is_set() and errors and errors[0] is None and after > before
    print("投递:", "成功" if ok else f"失败（{errors[0] if errors else '没有回应'}）", f"（通知中心里本应用的通知 {before} → {after}）", flush=True)
    return 0 if ok else 1
