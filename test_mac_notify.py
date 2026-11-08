"""macOS notifications: 环流's own banners, the osascript fallback, and the
tray's two notices (a quota reset, the Codex global-reset forecast)."""
import time
import unittest
from unittest.mock import Mock, patch

import mac_notify


class NotifierTest(unittest.TestCase):
    def test_without_an_app_bundle_it_falls_back_to_osascript(self):
        popen = Mock()
        notifier = mac_notify.Notifier(popen=popen)
        self.assertEqual(notifier.setup(bundle_id=""), mac_notify.UNAVAILABLE)
        self.assertEqual(notifier.post("标题", "正文"), "osascript")
        self.assertEqual(popen.call_args.args[0], ["osascript", "-e", 'display notification "正文" with title "标题"'])

    def test_native_posts_go_to_macos_whatever_answer_was_remembered(self):
        # A launch-time request can be answered with an error while the prompt
        # is still up; macOS enforces the person's setting, never a fallback.
        popen = Mock()
        notifier = mac_notify.Notifier(popen=popen)
        notifier.center = Mock()
        notifier._answered(False, None)
        self.assertEqual(notifier.state, mac_notify.DENIED)
        self.assertEqual(notifier.post("标题", "正文"), "native")
        notifier.center.addNotificationRequest_withCompletionHandler_.assert_called_once()
        popen.assert_not_called()

    def test_native_request_carries_title_body_and_sound(self):
        notifier = mac_notify.Notifier(popen=Mock())
        notifier.center = Mock()
        notifier._answered(True, None)
        done = Mock()
        self.assertEqual(notifier.post("Codex 额度已重置", "每周窗口：15% → 100%", done=done), "native")
        request, handler = notifier.center.addNotificationRequest_withCompletionHandler_.call_args.args
        self.assertEqual(str(request.content().title()), "Codex 额度已重置")
        self.assertEqual(str(request.content().body()), "每周窗口：15% → 100%")
        self.assertIsNotNone(request.content().sound())
        self.assertTrue(str(request.identifier()).startswith("circlow-"))
        handler(None)
        done.assert_called_once_with(None)

    def test_delegate_class_does_not_clash_with_the_menu_bars_delegate(self):
        # Objective-C class names are global: mac_menu registers `Delegate`,
        # and a notification delegate of that name failed to register, which
        # aborted the menu bar's launch (no icons, no readers).
        import objc
        import UserNotifications as UN
        from Foundation import NSObject
        try:
            objc.lookUpClass("Delegate")
        except objc.nosuchclass_error:
            type("Delegate", (NSObject,), {})           # what mac_menu.run_mac defines
        made = mac_notify.delegate_class(objc, NSObject, UN)
        self.assertEqual(made.__name__, "CirclowNotificationDelegate")
        self.assertIs(mac_notify.delegate_class(objc, NSObject, UN), made)   # defined once per process
        self.assertIsNotNone(made.alloc().init())

    def test_osascript_quoting(self):
        self.assertEqual(mac_notify.osascript_command('Codex "重置"', 'a\\b "c"')[2],
                         'display notification "a\\\\b \\"c\\"" with title "Codex \\"重置\\""')


class TrayNoticesTest(unittest.TestCase):
    """Both of the tray's notices reach the menu bar's notify()."""

    def tray(self):
        import mac_menu
        import tray_app
        icons = {key: mac_menu.MacIcon(key) for key in tray_app.PROVIDERS}
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.icons, tray.snapshots, tray.reset_outlook = icons, {}, None
        tray.scheduler = None
        tray.update_ui = lambda: None
        return tray

    def test_a_quota_reset_and_the_forecast_are_announced(self):
        import mac_menu
        import reset_watch
        import tray_app
        from quota_sources import QuotaSnapshot
        if "codex" not in tray_app.PROVIDERS:
            self.skipTest("Codex is not shown")
        tray = self.tray()
        now = time.time()
        old = QuotaSnapshot("codex", "Codex", 300, 70, 30, int(now + 600), now - 120, False, None,
                            secondary_window_minutes=10080, secondary_used_percent=85, secondary_remaining_percent=15,
                            secondary_resets_at=int(now + 3600))
        new = QuotaSnapshot("codex", "Codex", 300, 0, 100, int(now + 5 * 3600), now, False, None,
                            secondary_window_minutes=10080, secondary_used_percent=0, secondary_remaining_percent=100,
                            secondary_resets_at=int(now + 7 * 86400))
        with patch.object(mac_menu, "notify") as notify, patch.object(tray_app.reset_watch, "save"):
            tray.announce_resets("codex", old, new, now)
            tray.reset_alerted = "low"
            tray.apply_reset_outlook(reset_watch.ResetOutlook("willreset.com", "https://willreset.com/", 90, 95,
                                                              fetched_at=now), now)
        titles = [call.args[0] for call in notify.call_args_list]
        self.assertEqual(titles, ["Codex 额度已重置", "Codex 可能即将全局重置"])
        self.assertIn("每周窗口：15% → 100%", notify.call_args_list[0].args[1])
        self.assertIn("90%", notify.call_args_list[1].args[1])


    def test_the_test_button_reaches_the_menu_bar_and_its_notify(self):
        import os
        import tempfile
        import settings_mac
        tray = self.tray()
        with patch.object(tray, "notify") as notify:
            settings_mac.handle_menu_command(tray, {"name": "test-notify"})
        provider, title, _message = notify.call_args.args
        self.assertIn(provider, tray.icons)
        self.assertEqual(title, "环流 · 测试通知")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LOCALAPPDATA": folder}):
            import tray_app
            with patch.object(tray_app.QuotaTray, "__init__", lambda self, icons=None: None):
                settings = settings_mac.make_settings_tray()
            settings.test_notification()
            self.assertEqual(settings_mac.take_commands(settings_mac.MENU), [{"name": "test-notify"}])

if __name__ == "__main__":
    unittest.main()
