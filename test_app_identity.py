import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_identity
import claude_status
import settings_mac


class RenameTests(unittest.TestCase):
    """Everything carrying the old name (CodexQuotaTray) moves to Circlow."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_old_data_folder_is_renamed_once(self):
        old = self.root / "CodexQuotaTray"
        old.mkdir()
        (old / "state.json").write_text("{}", encoding="utf-8")
        self.assertEqual(app_identity.migrate_data_dir(self.root), "renamed")
        self.assertTrue((self.root / "Circlow" / "state.json").is_file())
        self.assertFalse(old.exists())
        self.assertEqual(app_identity.migrate_data_dir(self.root), "")

    def test_an_existing_new_folder_is_left_alone(self):
        (self.root / "CodexQuotaTray").mkdir()
        (self.root / "Circlow").mkdir()
        self.assertEqual(app_identity.migrate_data_dir(self.root), "")
        self.assertTrue((self.root / "CodexQuotaTray").exists())
        self.assertEqual(app_identity.migrate_data_dir(self.root / "nothing-here"), "")

    def test_a_folder_in_use_is_copied_instead(self):
        old = self.root / "CodexQuotaTray"
        old.mkdir()
        (old / "usage.json.gz").write_bytes(b"x")
        with mock.patch.object(app_identity.os, "rename", side_effect=PermissionError):
            self.assertEqual(app_identity.migrate_data_dir(self.root), "copied")
        self.assertEqual((self.root / "Circlow" / "usage.json.gz").read_bytes(), b"x")
        self.assertTrue(old.exists())

    def test_the_app_icon_is_found_in_assets_and_in_a_build(self):
        for suffix in (".ico", ".icns", ".png"):
            self.assertTrue(app_identity.icon_file(suffix).is_file(), suffix)
        (self.root / "circlow-fox-tray-mascot.ico").write_bytes(b"x")
        with mock.patch.object(app_identity.sys, "_MEIPASS", str(self.root), create=True):
            self.assertEqual(app_identity.icon_file(".ico"), self.root / "circlow-fox-tray-mascot.ico")
            self.assertIsNone(app_identity.icon_file(".icns"))

    def test_old_environment_settings_still_count(self):
        environ = {"CODEX_QUOTA_PROVIDERS": "codex", "CODEX_QUOTA_HERDR": "0", "CIRCLOW_HERDR": "1", "PATH": "x"}
        self.assertEqual(app_identity.adopt_legacy_env(environ), ["CIRCLOW_PROVIDERS"])
        self.assertEqual(environ["CIRCLOW_PROVIDERS"], "codex")
        self.assertEqual(environ["CIRCLOW_HERDR"], "1")            # the new name wins

    def test_the_claude_status_line_follows_the_rename(self):
        home, local = self.root / "claude", self.root / "local"
        (local / "Circlow").mkdir(parents=True)
        home.mkdir()
        old_command = f'powershell.exe -NoLogo -NoProfile -NonInteractive -File "{(local / "CodexQuotaTray" / "codexquotatray-statusline.ps1").as_posix()}"'
        (home / "settings.json").write_text(json.dumps({"model": "opus", "statusLine": {"type": "command", "command": old_command, "padding": 0}}),
                                            encoding="utf-8")
        (local / "Circlow" / "codexquotatray-statusline.ps1").write_text("old", encoding="utf-8")
        with mock.patch.object(claude_status.providers, "claude_home", return_value=home), \
                mock.patch.object(claude_status.providers, "_local_appdata", return_value=local):
            self.assertFalse(claude_status.bridge_installed())
            self.assertTrue(claude_status.refresh_receiver())
            self.assertTrue(claude_status.bridge_installed())
            self.assertFalse(claude_status.refresh_receiver())        # settled
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["statusLine"]["padding"], 0)
        self.assertIn("Circlow/circlow-statusline.ps1", settings["statusLine"]["command"])
        self.assertTrue((local / "Circlow" / "circlow-statusline.ps1").is_file())
        self.assertFalse((local / "Circlow" / "codexquotatray-statusline.ps1").exists())

    def test_someone_elses_status_line_is_never_touched(self):
        home = self.root / "claude"
        home.mkdir()
        (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "mine.sh"}}), encoding="utf-8")
        with mock.patch.object(claude_status.providers, "claude_home", return_value=home), \
                mock.patch.object(claude_status.providers, "_local_appdata", return_value=self.root):
            self.assertFalse(claude_status.refresh_receiver())
        self.assertIn("mine.sh", (home / "settings.json").read_text(encoding="utf-8"))

    def test_the_mac_login_item_is_swapped_for_the_new_one(self):
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        old = agents / "com.antiqiusama.codexquotatray.plist"
        with mock.patch.object(settings_mac, "launch_agent_path", return_value=agents / "com.antiqiusama.circlow.plist"), \
                mock.patch.object(settings_mac, "set_login_item") as set_item:
            self.assertFalse(settings_mac.migrate_login_item())
            old.write_bytes(b"plist")
            self.assertTrue(settings_mac.migrate_login_item())
        set_item.assert_called_once_with(True)
        self.assertFalse(old.exists())

    def test_the_windows_startup_entry_is_renamed(self):
        import tray_app
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        values = {"CodexQuotaTray": '"D:\\old\\CodexQuotaTray.exe"'}

        def set_value(command, name=tray_app.APP_NAME):
            if command is None:
                values.pop(name, None)
            else:
                values[name] = command
        with mock.patch.object(tray_app.QuotaTray, "_run_value", side_effect=lambda name=tray_app.APP_NAME: values.get(name)), \
                mock.patch.object(tray_app.QuotaTray, "_set_run_value", side_effect=set_value), \
                mock.patch.object(tray_app.QuotaTray, "startup_path", return_value=self.root / "none.cmd"), \
                mock.patch.object(tray_app, "startup_command", return_value='"D:\\new\\Circlow.exe"'):
            tray.migrate_startup()
        self.assertEqual(values, {"Circlow": '"D:\\new\\Circlow.exe"'})


if __name__ == "__main__":
    unittest.main()
