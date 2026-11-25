import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import accounts
import quota_sources
from local_sync import LocalSync


def jwt(claims):
    return "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"


def codex_file(account, email):
    return {"tokens": {"account_id": account, "access_token": "SECRET-A", "refresh_token": "SECRET-R", "id_token": jwt(
        {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}})}}


class ReadingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for patcher in (patch.dict(os.environ, {"CODEX_HOME": folder.name, "CLAUDE_CONFIG_DIR": folder.name}),
                        patch.object(accounts, "_CACHE", {}), patch.object(accounts.Path, "home", return_value=self.folder)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.folder / ".grok").mkdir()

    def write(self, name, data):
        path = self.folder / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def test_each_agent_names_its_account(self):
        self.write("auth.json", codex_file("acc-1", "someone@example.com"))
        self.write(".claude.json", {"oauthAccount": {"accountUuid": "u", "organizationUuid": "o", "emailAddress": "abc@example.com"}})
        self.write(".grok/auth.json", {"https://issuer::client": {"user_id": "g", "email": "grokuser@example.com", "key": "SECRET-K"}})
        codex, claude, grok = (accounts.current(key) for key in ("codex", "claude", "grok"))
        self.assertEqual((codex.name, codex.plan), ("som•••ne@example.com", "Plus"))
        self.assertEqual((claude.name, grok.name), ("a•••@example.com", "gro•••er@example.com"))
        self.assertEqual(len({codex.key, claude.key, grok.key}), 3)
        for account in (codex, claude, grok):
            self.assertNotRegex(repr(account), "SECRET|someone|grokuser")
        self.assertIsNone(accounts.current("gemini"))

    def test_signed_out_or_unreadable_is_no_account(self):
        self.assertIsNone(accounts.key("codex"))
        for text in ("{", "[]", "{}", json.dumps({"tokens": {"id_token": "bad"}})):
            self.write("auth.json", text)
            self.assertIsNone(accounts.key("codex"))
        self.write("auth.json", {"OPENAI_API_KEY": "sk-SECRET"})
        self.assertEqual(accounts.current("codex").name, "API Key")
        self.write(".claude.json", {"oauthAccount": None})
        self.assertIsNone(accounts.key("claude"))

    def test_the_file_is_read_again_only_when_it_changes(self):
        path = self.write("auth.json", codex_file("acc-1", "someone@example.com"))
        first = accounts.key("codex")
        with patch.object(accounts.json, "loads", side_effect=AssertionError("not changed")):
            self.assertEqual(accounts.key("codex"), first)
        self.write("auth.json", codex_file("acc-2", "another@example.com"))
        os.utime(path, (1, 1))
        self.assertNotEqual(accounts.key("codex"), first)

    def test_gemini_is_asked_for(self):
        with patch.object(accounts.cli_usage, "run_agy_usage", return_value={"account": "k", "account_name": "x•••@y"}):
            self.assertEqual(accounts.read("gemini"), accounts.Account("k", "x•••@y"))
        with patch.object(accounts.cli_usage, "run_agy_usage", return_value={"account": None}):
            self.assertIsNone(accounts.read("gemini"))


    def test_the_settings_line(self):
        from settings_window import account_text
        account = accounts.Account("k", "x•••@y", "Plus")
        self.assertEqual(account_text(account), "x•••@y · Plus")
        self.assertEqual(account_text(account, "登录未完成"), "x•••@y · Plus · 登录未完成")
        self.assertEqual((account_text(None), account_text(None, "正在读取账号…")), ("未登录", "正在读取账号…"))


class SwitchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patcher = patch.object(accounts.app_identity, "data_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_agents_own_sign_in_opens_in_ghostty(self):
        import herdr_source
        for provider, words in (("codex", "login"), ("claude", "auth login"), ("grok", "login"), ("gemini", "acp --login")):
            popen = Mock()
            accounts.marker(provider).parent.mkdir(exist_ok=True)
            accounts.marker(provider).write_text("0")                       # left over: not this sign-in's result
            with patch.object(accounts, "executable", return_value=Path("/x y/tool")), \
                    patch.object(accounts.sys, "platform", "darwin"), \
                    patch.object(herdr_source, "ghostty_app", return_value="/Applications/Ghostty.app"):
                accounts.switch(provider, popen)
            args = popen.call_args.args[0]
            self.assertEqual(args[:3], ["open", "-na", "/Applications/Ghostty.app"])
            self.assertEqual(args[-3:-1], ["/bin/zsh", "-lc"])
            self.assertIn(f"tool' {words};", args[-1])
            self.assertFalse(accounts.marker(provider).exists())

    def test_windows_runs_it_in_its_own_console(self):
        popen = Mock()
        with patch.object(accounts, "executable", return_value=Path("codex.cmd")), patch.object(accounts.sys, "platform", "win32"):
            accounts.switch("codex", popen)
        script = (self.folder / "account-login" / "codex.cmd")
        self.assertEqual(popen.call_args.args[0][-1], str(script))
        text = script.read_bytes().decode("utf-8")
        self.assertIn("call codex.cmd login\r\n", text)
        self.assertIn(f'echo %errorlevel%> "{accounts.marker("codex")}"', text)

    def test_not_installed(self):
        with patch.object(accounts, "executable", return_value=None), self.assertRaises(FileNotFoundError):
            accounts.switch("codex", Mock(side_effect=AssertionError("nothing to open")))

    @unittest.skipUnless(sys.platform == "darwin", "zsh")
    def test_the_script_notes_how_the_sign_in_ended(self):
        done = self.folder / "it's done"
        for command, code in ((["/usr/bin/true"], "0"), (["/usr/bin/false"], "1")):
            subprocess.run(["/bin/zsh", "-c", accounts.login_script(command, done)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            self.assertEqual(done.read_text().strip(), code)

    def test_waiting_for_the_sign_in(self):
        done = accounts.marker("grok")
        done.parent.mkdir()
        for text, result in (("0\n", True), ("1\n", False)):
            done.write_text(text)
            self.assertIs(accounts.wait("grok", Mock(side_effect=AssertionError("already done"))), result)
            self.assertFalse(done.exists())
        self.assertIsNone(accounts.wait("grok", lambda seconds: True))      # the window is closing


class FollowingTests(unittest.TestCase):
    def tray(self, account="old"):
        tray = LocalSync()
        tray.snapshots = {"codex": quota_sources.QuotaSnapshot(
            "codex", "Codex", 300, 60, 40, 9000, 100, False, None, account=account, secondary_resets_at=500000)}
        tray.scheduler, tray.wake, tray.history = Mock(), threading.Event(), Mock()
        return tray

    def test_a_new_sign_in_is_read_at_once_and_only_once(self):
        tray = self.tray()
        with patch.object(accounts, "key", return_value="new"):
            for now in (10, 11, 20, 30):
                tray.follow_accounts(now)
        self.assertEqual([c.args[0] for c in tray.scheduler.request.call_args_list], [["codex"]])
        self.assertTrue(tray.wake.is_set())
        same = self.tray()
        with patch.object(accounts, "key", return_value="old"):
            same.follow_accounts(10)
        with patch.object(accounts, "key", return_value=None):              # signed out
            same.follow_accounts(20)
        same.scheduler.request.assert_not_called()

    def test_the_trend_starts_over_with_the_account(self):
        tray = self.tray()
        old, fresh = tray.snapshots["codex"], SimpleNamespace(account="new", updated_at=200)
        tray.account_changed("codex", old, fresh)
        tray.history.clear.assert_called_once_with("codex")
        tray.account_changed("codex", old, SimpleNamespace(account=None, updated_at=300))
        tray.history.clear.assert_called_once()

    def test_codex_sessions_of_the_account_before_are_left_out(self):
        tray = self.tray("new")
        shown = tray.snapshots["codex"]

        def ours(at, week):
            return tray.codex_record_is_ours(SimpleNamespace(updated_at=at, secondary_resets_at=week), shown)
        self.assertTrue(ours(50, 900000))                                   # no switch: as before
        with patch.dict(quota_sources.ACCOUNT_SINCE, {"codex": 150.0}, clear=True):
            self.assertFalse(ours(120, 500000))
            self.assertFalse(ours(200, 900000))
            self.assertTrue(ours(200, 500060))


if __name__ == "__main__":
    unittest.main()
