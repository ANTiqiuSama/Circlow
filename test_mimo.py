"""Xiaomi MiMo Token Plan: Credits, console parsing, ledger, tracker, sources."""
import json
import os
import sys
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import mimo_usage
import providers
import quota_sources
from mimo_usage import (BEIJING, LocalLedger, LoginRequired, MimoTracker, PlanStatus, WebSocket, credits,
                        credits_text, manual_plan, parse_plan, parse_time, platform_cookies, price_for, redact)
from tps_sources import Observation, TpsMonitor

DAY = 86400
NOON = datetime(2026, 6, 15, 12, 0, tzinfo=BEIJING).timestamp()
NIGHT = datetime(2026, 6, 15, 3, 0, tzinfo=BEIJING).timestamp()


def obs(ident, when, source="Claude Code", model="mimo-v2.6-pro", output=1000, miss=10_000, hit=100_000, provider="mimo"):
    return Observation(ident, source, "s", provider, model, when, output, None, "", True, None, miss, hit)


def ok(data):
    return {"code": 0, "message": "ok", "data": data}


USAGE = ok({"usage": {"items": [
    {"name": "plan_total_token", "used": 2.0e9, "limit": 10.0e9, "percent": 20},
    {"name": "compensation_total_token", "used": 0, "limit": 1.0e9, "percent": 0},
]}})
DETAIL = ok({"planName": "Pro", "planCode": "pro_year", "currentPeriodEnd": "2027-01-01 00:00:00",
             "expired": False, "enableAutoRenew": True, "settlementType": "YEARLY_AUTO"})


class CreditTests(unittest.TestCase):
    def test_price_table_and_night_discount(self):
        self.assertEqual(credits("mimo-v2.6-pro", 100, 10, 1, NOON), (100 * 2.5 + 10 * 300 + 600, True))
        value, exact = credits("xiaomi/mimo-v2.5", 0, 0, 10, NIGHT)
        self.assertAlmostEqual(value, 10 * 200 * 0.8)
        self.assertTrue(exact)

    def test_unknown_models_are_charged_as_upper_bound(self):
        self.assertEqual(price_for("mimo-v3-preview"), (mimo_usage.PRICES["mimo-v2.6-pro"], False))
        self.assertEqual(price_for("mimo-v9-flash")[0], mimo_usage.PRICES["mimo-v2.6-flash"])
        self.assertEqual(price_for("mimo-v2.5-pro")[0], (2.5, 300, 600))  # longest prefix wins

    def test_text(self):
        self.assertEqual(credits_text(None), "—")
        self.assertEqual(credits_text(2.5e10), "250.00 亿")
        self.assertEqual(credits_text(5.6e7), "5600 万")
        self.assertEqual(credits_text(12_345), "1.2 万")
        self.assertEqual(credits_text(42), "42")


class ConsoleParsingTests(unittest.TestCase):
    def test_usage_items_and_detail(self):
        plan = parse_plan(USAGE, DETAIL, now=100.0)
        self.assertEqual((plan.total, plan.used, plan.plan_name, plan.settlement), (11.0e9, 2.0e9, "Pro", "YEARLY_AUTO"))
        end = datetime(2027, 1, 1, tzinfo=BEIJING).timestamp()
        self.assertEqual(plan.period_end, end)
        self.assertEqual(plan.period_start, datetime(2026, 1, 1, tzinfo=BEIJING).timestamp())
        self.assertEqual(plan.fetched_at, 100.0)

    def test_unwrapped_percent_only_and_team_payloads(self):
        plan = parse_plan({"items": [{"name": "x", "used": 25, "percent": 25}]}, {"currentPeriodEnd": "2027-06-15T00:00:00+08:00"})
        self.assertEqual((plan.total, plan.used), (100, 25))
        self.assertEqual(plan.period_start, datetime(2026, 6, 15, tzinfo=BEIJING).timestamp())
        team = parse_plan(ok({"creditsTotal": 500, "creditsUsed": 50, "usedPercent": 10}), ok(None))
        self.assertEqual((team.total, team.used), (500, 50))

    def test_always_one_yearly_pool(self):
        # A monthly billing cycle is ignored: the plan expiry ends a one-year period.
        detail = {"settlementType": "MONTHLY_AUTO", "currentPeriodStart": "2026-06-01", "currentPeriodEnd": "2026-07-01",
                  "expireTime": "2027-03-01 00:00:00"}
        plan = parse_plan(USAGE, detail)
        self.assertEqual(plan.period_end, datetime(2027, 3, 1, tzinfo=BEIJING).timestamp())
        self.assertEqual(plan.period_start, datetime(2026, 3, 1, tzinfo=BEIJING).timestamp())
        only_month = parse_plan(USAGE, {"currentPeriodStart": "2026-06-01", "currentPeriodEnd": "2026-07-01"})
        self.assertEqual(only_month.period_start, datetime(2025, 7, 1, tzinfo=BEIJING).timestamp())

    def test_errors(self):
        with self.assertRaises(LoginRequired):
            parse_plan({"code": 401, "message": "Unauthorized"}, DETAIL)
        with self.assertRaises(LoginRequired):
            parse_plan({"code": 10001, "message": "请先登录"}, DETAIL)
        with self.assertRaises(RuntimeError):
            parse_plan({"code": 500, "message": "busy"}, DETAIL)
        with self.assertRaises(ValueError):
            parse_plan(ok({"usage": {"items": []}}), ok(None))

    def test_time_formats(self):
        self.assertEqual(parse_time(1_800_000_000_000), 1_800_000_000)
        self.assertEqual(parse_time("1800000000"), 1_800_000_000)
        self.assertEqual(parse_time("2026-06-15 12:00"), NOON)
        self.assertEqual(parse_time("2026-06-15T04:00:00Z"), NOON)
        self.assertIsNone(parse_time("soon"))
        self.assertIsNone(parse_time(None))

    def test_leap_day_year_start(self):
        end = datetime(2028, 2, 29, tzinfo=BEIJING).timestamp()
        self.assertEqual(mimo_usage._period_start(end, "YEARLY_AUTO", ""), end - 365 * DAY)

    def test_redact_keeps_numbers_and_plan_fields(self):
        data = redact({"planName": "Pro", "userId": "123", "mobile": "138", "usage": {"items": [{"name": "a", "used": 1}]}, "uid": 9})
        self.assertEqual(data, {"planName": "Pro", "userId": "<redacted>", "mobile": "<redacted>",
                                "usage": {"items": [{"name": "a", "used": 1}]}, "uid": 9})

    def test_platform_cookies_never_include_account_domain(self):
        jar = platform_cookies([
            {"name": "sid", "value": "1", "domain": ".xiaomimimo.com"},
            {"name": "p", "value": "2", "domain": "platform.xiaomimimo.com"},
            {"name": "passToken", "value": "3", "domain": ".account.xiaomi.com"},
            {"name": "other", "value": "4", "domain": "evil-xiaomimimo.com"},
        ])
        self.assertEqual(jar, {"sid": "1", "p": "2"})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    @unittest.skipUnless(os.name == "nt", "console session encryption requires Windows DPAPI")
    def test_cookie_roundtrip_and_logout(self):
        path = self.dir / "s.bin"
        mimo_usage.save_cookies({"a": "b"}, path)
        self.assertNotIn(b'"b"', path.read_bytes())
        self.assertEqual(mimo_usage.load_cookies(path), {"a": "b"})
        self.assertEqual(mimo_usage.load_cookies(self.dir / "missing"), {})

    @unittest.skipUnless(os.name == "nt", "console session encryption requires Windows DPAPI")
    def test_session_format_and_first_format_compatibility(self):
        path = self.dir / "s.bin"
        mimo_usage.save_session({"cookies": {"sid": "1"}, "headers": {"Authorization": "Bearer t"}, "api_base": "https://x"}, path)
        self.assertEqual(mimo_usage.load_session(path)["headers"], {"Authorization": "Bearer t"})
        mimo_usage.save_cookies({"sid": "2"}, path)   # keeps the captured header
        self.assertEqual(mimo_usage.load_session(path), {"cookies": {"sid": "2"}, "headers": {"Authorization": "Bearer t"}, "api_base": "https://x"})
        path.write_bytes(mimo_usage._protect(json.dumps({"old": "jar"}).encode()))
        self.assertEqual(mimo_usage.load_session(path)["cookies"], {"old": "jar"})

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "Linux must refuse unencrypted console sessions")
    def test_non_windows_refuses_console_session_persistence(self):
        with self.assertRaises(OSError):
            mimo_usage.save_session({"cookies": {"sid": "secret"}}, self.dir / "s.bin")
        self.assertFalse((self.dir / "s.bin").exists())

    @unittest.skipUnless(sys.platform == "darwin", "the macOS keychain")
    def test_macos_keeps_the_session_in_the_keychain_and_only_a_marker_on_disk(self):
        store = {}
        path = self.dir / "s.bin"
        with patch.object(mimo_usage, "_keychain_store", lambda account, data: store.__setitem__(account, data)), \
                patch.object(mimo_usage, "_keychain_load", lambda account: store.get(account)):
            mimo_usage.save_session({"cookies": {"sid": "secret"}, "headers": {"Authorization": "Bearer t"}, "api_base": "https://x"}, path)
            self.assertEqual(path.read_bytes(), mimo_usage.KEYCHAIN_MARKER)          # nothing secret on disk
            self.assertNotIn(b"secret", path.read_bytes())
            self.assertEqual(mimo_usage.load_session(path)["cookies"], {"sid": "secret"})
            store.clear()                                                           # gone from the keychain
            self.assertEqual(mimo_usage.load_session(path)["cookies"], {})
            path.write_bytes(b"not a marker")                                       # a Windows file copied over
            self.assertEqual(mimo_usage.load_session(path)["cookies"], {})

    def test_browser_lookup_covers_macos_chromium_browsers(self):
        with patch.object(mimo_usage.sys, "platform", "darwin"), patch.dict(os.environ, {"CIRCLOW_MIMO_BROWSER": ""}), \
                patch.object(mimo_usage.os.path, "isfile", lambda path: path.endswith("Google Chrome")):
            self.assertEqual(mimo_usage.edge_path(), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    def test_captured_api_keeps_only_console_token_headers(self):
        events = [
            {"method": "Network.requestWillBeSent", "params": {"request": {"url": "https://cdn.example.com/api/x", "headers": {"Authorization": "no"}}}},
            {"method": "Network.requestWillBeSent", "params": {"request": {"url": "https://api.xiaomimimo.com/api/v1/tokenPlan/usage",
             "headers": {"Authorization": "Bearer t", "Accept": "application/json", "sec-ch-ua": "x", "x-timeZone": "Asia/Shanghai", "x-csrf": "c"}}}},
            {"method": "Page.loadEventFired", "params": {}},
        ]
        self.assertEqual(mimo_usage.captured_api(events), ("https://api.xiaomimimo.com", {"Authorization": "Bearer t", "x-csrf": "c"}))

    def test_manual_config(self):
        path = self.dir / "mimo.json"
        self.assertIsNone(manual_plan(path))
        path.write_text(json.dumps({"total_credits": 1e9, "period_end": "2027-01-01"}), encoding="utf-8")
        plan = manual_plan(path)
        self.assertEqual((plan.total, plan.used, plan.source), (1e9, 0.0, "manual"))
        self.assertEqual(plan.period_start, datetime(2026, 1, 1, tzinfo=BEIJING).timestamp())

    def test_ledger_counts_mimo_once_and_persists(self):
        path = self.dir / "ledger.json"
        ledger = LocalLedger(path)
        now = time.time()
        items = [obs("a", now - 10), obs("a", now - 10), obs("b", now - 5, source="Codex"),
                 obs("c", now - 5, provider="claude", model="claude-opus")]
        self.assertEqual(ledger.ingest(items), 2)
        self.assertEqual(ledger.ingest(items), 0)
        one, _ = credits("mimo-v2.6-pro", 100_000, 10_000, 1000, now - 10)
        self.assertAlmostEqual(ledger.since(now - 60), 2 * one, delta=1)
        self.assertEqual(set(ledger.by_source(now - 60)), {"Claude Code", "Codex"})
        ledger.save(force=True)
        again = LocalLedger(path)
        self.assertEqual(again.ingest(items), 0)
        self.assertAlmostEqual(again.since(now - 60), ledger.since(now - 60))
        self.assertFalse(again.estimated(now - 60))
        again.ingest([obs("d", now, model="mimo-v3")])
        self.assertTrue(again.estimated(now - 60))


class TrackerTests(unittest.TestCase):
    def test_daily_usage_cache_and_local_midnight_rollover(self):
        now = mimo_usage._local_midnight(self.now) + 12 * 3600
        self.tracker.refresh(lambda: self.plan(used=40e9, fetched=now - 60))
        self.tracker.refresh(lambda: self.plan(used=41e9, fetched=now - 1))
        view = self.tracker.view(now)
        self.assertEqual(view.today_used, 1e9)
        snapshot = quota_sources.mimo_snapshot(view)
        self.assertEqual(snapshot.today_date, datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
        with patch.object(quota_sources, "CACHE_PATH", self.dir / "state.json"):
            quota_sources.save_cached_snapshots({"mimo": snapshot})
            restored = quota_sources.load_cached_snapshots()["mimo"]
        self.assertEqual(restored.today_used, 1e9)
        self.assertEqual(restored.today_date, snapshot.today_date)
        tomorrow = self.tracker.view(now + DAY)
        self.assertEqual(tomorrow.today_used, 0)
        self.assertNotEqual(tomorrow.day, view.day)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = LocalLedger(self.dir / "ledger.json")
        self.tracker = MimoTracker(self.ledger, self.dir / "days.json")
        self.now = time.time()

    def plan(self, used=40e9, fetched=None):
        return PlanStatus(100e9, used, "Max", period_start=self.now - 100 * DAY, period_end=self.now + 100 * DAY,
                          fetched_at=fetched if fetched is not None else self.now - 60)

    def test_anchor_plus_local_increment_and_today_budget(self):
        self.tracker.refresh(lambda: self.plan())
        self.ledger.ingest([obs("before", self.now - 120, output=0, miss=0, hit=0), obs("after", self.now - 1, output=1_000_000, miss=0, hit=0)])
        view = self.tracker.view(self.now)
        self.assertAlmostEqual(view.used, 40e9 + 600e6 * (0.8 if mimo_usage.is_night(self.now - 1) else 1))
        self.assertAlmostEqual(view.remaining, 100e9 - view.used)
        self.assertAlmostEqual(view.time_percent, 50, delta=0.1)
        self.assertGreaterEqual(view.today_used, view.local_today)
        days_left = (view.period_end - mimo_usage._local_midnight(self.now)) / DAY
        self.assertAlmostEqual(view.today_budget, (view.remaining + view.today_used) / days_left, delta=1)
        self.assertLess(view.today_budget_left_percent, 100)
        self.assertAlmostEqual(view.pace_daily, view.used / 100, delta=view.used / 1000)
        self.assertIsNone(view.error)
        self.assertEqual(json.loads((self.dir / "days.json").read_text())[datetime.fromtimestamp(self.now - 60).strftime("%Y-%m-%d")], 40e9)

    def test_other_devices_show_up_through_console_delta(self):
        self.tracker.refresh(lambda: self.plan(used=40e9, fetched=self.now - 30))
        self.tracker.refresh(lambda: self.plan(used=41e9, fetched=self.now - 1))
        view = self.tracker.view(self.now)
        self.assertEqual(view.local_today, 0)
        self.assertAlmostEqual(view.today_used, 1e9)

    def test_fast_pace_predicts_early_exhaustion(self):
        self.tracker.refresh(lambda: self.plan(used=90e9))
        view = self.tracker.view(self.now)
        self.assertLess(view.exhausts_at, view.period_end)

    def test_login_error_keeps_last_numbers_and_snapshot_is_stale(self):
        self.tracker.refresh(lambda: self.plan())
        def expired():
            raise LoginRequired("HTTP 401")
        with patch.object(mimo_usage, "CONFIG_PATH", self.dir / "none.json"):
            self.tracker.refresh(expired)
        view = self.tracker.view(self.now)
        self.assertIsNotNone(view.remaining)
        self.assertIn("未登录", view.error)
        snapshot = quota_sources.mimo_snapshot(view)
        self.assertTrue(snapshot.stale)
        self.assertIsNone(snapshot.error)
        self.assertIsNone(snapshot.secondary_window_minutes)   # no daily-budget window
        self.assertAlmostEqual(snapshot.remaining_percent, 60, delta=0.1)

    def test_nothing_known_is_an_error_snapshot(self):
        with patch.object(mimo_usage, "CONFIG_PATH", self.dir / "none.json"):
            self.tracker.refresh(lambda: (_ for _ in ()).throw(LoginRequired("尚未登录")))
        snapshot = quota_sources.mimo_snapshot(self.tracker.view(self.now))
        self.assertIsNone(snapshot.remaining_percent)
        self.assertIn("未登录", snapshot.error)
        self.assertFalse(snapshot.stale)

    def test_manual_mode_uses_local_ledger_since_period_start(self):
        config = self.dir / "mimo.json"
        start = datetime.fromtimestamp(self.now - 10 * DAY, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(self.now + 20 * DAY, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        config.write_text(json.dumps({"total_credits": 1e9, "period_start": start, "period_end": end}), encoding="utf-8")
        self.ledger.ingest([obs("m", self.now - 5, output=100_000, miss=0, hit=0)])
        with patch.object(mimo_usage, "CONFIG_PATH", config):
            self.tracker.refresh(lambda: (_ for _ in ()).throw(LoginRequired("尚未登录")))
        view = self.tracker.view(self.now)
        self.assertEqual(view.source, "manual")
        self.assertAlmostEqual(view.used, self.ledger.since(self.now - 11 * DAY))
        self.assertGreater(view.used, 0)


class WebSocketTests(unittest.TestCase):
    def test_masked_frames_roundtrip(self):
        left, right = socket.socketpair()
        client = WebSocket.__new__(WebSocket)
        client.sock, client.buffer, client.next_id = left, b"", 0
        for payload in (b"x" * 10, b"y" * 300, b"z" * 70_000):
            sender = threading.Thread(target=right.sendall, args=(WebSocket.frame(payload),))
            sender.start()
            self.assertEqual(client.receive(), payload.decode())
            sender.join()
        left.close()
        right.close()

    def test_call_matches_reply_id(self):
        left, right = socket.socketpair()
        client = WebSocket.__new__(WebSocket)
        client.sock, client.buffer, client.next_id = left, b"", 0

        def server():
            # Unmasked server frames: an event first, then the reply.
            for message in ({"method": "Event"}, {"id": 1, "result": {"cookies": []}}):
                body = json.dumps(message).encode()
                right.sendall(bytes([0x81, len(body)]) + body)
        thread = threading.Thread(target=server)
        thread.start()
        self.assertEqual(client.call("Storage.getCookies"), {"cookies": []})
        thread.join()
        left.close()
        right.close()


class LocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.now = time.time()
        self.monitor = TpsMonitor(home=self.home)
        self.monitor.started_at = self.now - 100

    def test_claude_code_routed_to_mimo_counts_as_mimo_with_input_split(self):
        message = {"id": "m1", "model": "mimo-v2.6-pro", "stop_reason": "end_turn",
                   "usage": {"output_tokens": 50, "input_tokens": 7, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 900}}
        self.monitor._claude_record({"type": "assistant", "timestamp": self.now - 1, "message": message}, "sess", self.now)
        self.monitor._claude_flush(self.now)
        item = self.monitor.events[-1]
        self.assertEqual((item.provider, item.output_tokens, item.input_tokens, item.cached_tokens), ("mimo", 50, 10, 900))

    def test_codex_record_carries_uncached_and_cached_input(self):
        self.monitor.context["s"] = {"model": "mimo-v2.6-pro", "source": "Codex"}
        self.monitor._codex_record({"timestamp": self.now - 1, "type": "token_usage_record", "payload": {
            "thread_id": "s", "response_id": "r", "usage": {"output_tokens": 40, "input_tokens": 1000, "cached_input_tokens": 800}}}, "s", self.now)
        item = self.monitor.events[-1]
        self.assertEqual((item.provider, item.input_tokens, item.cached_tokens), ("mimo", 200, 800))

    def test_mimo_code_database(self):
        folder = self.home / ".local/share/mimocode"
        folder.mkdir(parents=True)
        db = sqlite3.connect(folder / "mimocode.db")
        db.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        ms = int(self.now * 1000)
        rows = [
            ("a", {"role": "assistant", "modelID": "mimo-v2.6-pro", "providerID": "xiaomi-token-plan",
                   "tokens": {"input": 100, "output": 30, "reasoning": 20, "cache": {"read": 400, "write": 5}}, "time": {"completed": ms - 500}}),
            ("b", {"role": "user", "tokens": {}}),
            ("c", {"role": "assistant", "modelID": "mimo-auto", "providerID": "mimo-free",
                   "tokens": {"output": 9}, "time": {"completed": ms - 400}}),
            ("d", {"role": "assistant", "modelID": "mimo-v2.6-pro", "tokens": {"output": 5}, "time": {}}),
        ]
        db.executemany("INSERT INTO message VALUES (?, 'sess', ?, ?, ?)", [(i, ms - 1000, ms - 300, json.dumps(d)) for i, d in rows])
        db.commit()
        db.close()
        self.monitor._mimocode(self.now)
        by_model = {item.model: item for item in self.monitor.events}
        self.assertEqual(set(by_model), {"mimo-v2.6-pro", "mimo-auto"})
        plan = by_model["mimo-v2.6-pro"]
        self.assertEqual((plan.provider, plan.source, plan.output_tokens, plan.input_tokens, plan.cached_tokens), ("mimo", "MiMo Code", 50, 105, 400))
        self.assertEqual(by_model["mimo-auto"].provider, "other")
        self.monitor._mimocode(self.now)  # unchanged file: nothing new
        self.assertEqual(len(self.monitor.events), 2)
        self.assertEqual(self.monitor.health["MiMo Code"]["status"], "已接入")


class BrowserStartupTests(unittest.TestCase):
    def test_handoff_waits_for_late_port_file(self):
        with tempfile.TemporaryDirectory() as folder:
            port = Path(folder) / 'DevToolsActivePort'
            def publish(_seconds):
                port.write_text('12345\n/devtools/browser/test\n')
            with patch.object(mimo_usage.time, 'sleep', side_effect=publish):
                self.assertEqual(mimo_usage._wait_browser_port(port, time.monotonic() + 5),
                                 ['12345', '/devtools/browser/test'])

    def test_missing_port_times_out(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '调试端口'):
                mimo_usage._wait_browser_port(Path(folder) / 'missing', time.monotonic() - 1)


class MimoTpsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.now = time.time()
        self.monitor = TpsMonitor(home=self.home)
        self.monitor.started_at = self.now - 100

    def test_mimo_code_parts_give_streaming_tps_and_live_activity(self):
        folder = self.home / ".local/share/mimocode"
        folder.mkdir(parents=True)
        db = sqlite3.connect(folder / "mimocode.db")
        db.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        db.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        ms = int(self.now * 1000)
        done = {"role": "assistant", "modelID": "mimo-v2.6-pro", "providerID": "xiaomi",
                "tokens": {"output": 300, "reasoning": 100, "cache": {}}, "time": {"created": ms - 20000, "completed": ms - 1000}}
        live = {"role": "assistant", "modelID": "mimo-v2.6-pro", "tokens": {}, "time": {"created": ms - 2000}}
        db.execute("INSERT INTO message VALUES ('m1', 's1', ?, ?, ?)", (ms - 20000, ms - 1000, json.dumps(done)))
        db.execute("INSERT INTO message VALUES ('m2', 's2', ?, ?, ?)", (ms - 2000, ms - 500, json.dumps(live)))
        parts = [  # 5 s before the first token (not counted); 2 s thinking, 6 s text, tool args until 9 s
            {"type": "step-start"},
            {"type": "reasoning", "time": {"start": ms - 15000, "end": ms - 13000}},
            {"type": "text", "time": {"start": ms - 13000, "end": ms - 7000}},
            {"type": "tool", "state": {"status": "completed", "time": {"start": ms - 6000, "end": ms - 1500}}},
        ]
        db.executemany("INSERT INTO part VALUES (?, 'm1', 's1', 0, 0, ?)", [(f"p{i}", json.dumps(p)) for i, p in enumerate(parts)])
        db.commit()
        db.close()
        self.monitor._mimocode(self.now)
        item = self.monitor.events[-1]
        self.assertEqual((item.provider, item.output_tokens), ("mimo", 400))
        self.assertAlmostEqual(item.native_tps, 400 / 9, places=1)
        self.assertIn("MiMo Code 流式时间", item.basis)
        self.assertEqual(self.monitor.activity["s2"][0], "mimo")
        data = self.monitor.snapshot(self.now)["providers"]["mimo"]
        self.assertAlmostEqual(data["display_tps"], 44.4, places=1)

    def test_mimo_code_without_part_table_still_counts(self):
        folder = self.home / ".local/share/mimocode"
        folder.mkdir(parents=True)
        db = sqlite3.connect(folder / "mimocode.db")
        db.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        ms = int(self.now * 1000)
        db.execute("INSERT INTO message VALUES ('m', 's', 0, ?, ?)", (ms - 100, json.dumps(
            {"role": "assistant", "modelID": "mimo-v2.6-pro", "tokens": {"output": 50}, "time": {"completed": ms - 200}})))
        db.commit()
        db.close()
        self.monitor._mimocode(self.now)
        self.assertIsNone(self.monitor.events[-1].native_tps)

    def test_claude_code_routed_to_mimo_gets_end_to_end_rate(self):
        self.monitor._claude_record({"type": "user", "timestamp": self.now - 11, "message": {}}, "sess", self.now)
        message = {"id": "m1", "model": "mimo-v2.6-pro", "stop_reason": "end_turn", "usage": {"output_tokens": 500}}
        self.monitor._claude_record({"type": "assistant", "timestamp": self.now - 1, "message": message}, "sess", self.now)
        self.monitor._claude_flush(self.now)
        item = self.monitor.events[-1]
        self.assertEqual(item.native_tps, 50.0)
        self.assertIn("含首字延迟", item.basis)

    def test_real_claude_models_still_have_no_invented_tps(self):
        self.monitor._claude_record({"type": "user", "timestamp": self.now - 11, "message": {}}, "sess", self.now)
        message = {"id": "m2", "model": "claude-opus-4", "stop_reason": "end_turn", "usage": {"output_tokens": 500}}
        self.monitor._claude_record({"type": "assistant", "timestamp": self.now - 1, "message": message}, "sess", self.now)
        self.monitor._claude_flush(self.now)
        self.assertIsNone(self.monitor.events[-1].native_tps)

    def test_codex_with_mimo_model_uses_client_stream_time(self):
        self.monitor.context["s"] = {"model": "mimo-v2.6-pro", "source": "Codex"}
        self.monitor.client_stream_starts["s"] = [self.now - 11]
        self.monitor._codex_record({"timestamp": self.now - 1, "type": "token_usage_record", "payload": {
            "thread_id": "s", "response_id": "r", "usage": {"output_tokens": 200, "input_tokens": 10, "cached_input_tokens": 0}}}, "s", self.now)
        self.assertEqual(self.monitor.events[-1].native_tps, 20.0)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        choices = patch.object(providers, "provider_choices", return_value={})
        choices.start()
        self.addCleanup(choices.stop)

    def test_claude_routed_to_mimo_is_not_a_claude_subscription(self):
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": home, "ANTHROPIC_BASE_URL": ""}):
            (Path(home) / "projects").mkdir()
            self.assertFalse(providers.claude_routes_to_mimo())
            (Path(home) / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://token-plan-cn.xiaomimimo.com/anthropic"}}))
            self.assertTrue(providers.claude_routes_to_mimo())
            with patch.object(providers, "find_claude", return_value=None):
                self.assertFalse(providers.get("claude").available())

    def test_mimo_detection_and_registry(self):
        spec = providers.get("mimo")
        self.assertEqual((spec.letter, spec.label, spec.launch[0]), ("M", "Xiaomi MiMo", "url"))
        self.assertEqual(providers.enabled_keys("codex,mimo"), ["codex", "mimo"])
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"LOCALAPPDATA": root, "CODEX_HOME": root,
                                                                            "CLAUDE_CONFIG_DIR": root, "ANTHROPIC_BASE_URL": ""}), \
                patch.object(providers.Path, "home", return_value=Path(root)):
            self.assertFalse(providers._mimo_used())
            (Path(root) / "config.toml").write_text('base_url = "https://token-plan-cn.xiaomimimo.com/v1"\n')
            self.assertTrue(providers._mimo_used())


if __name__ == "__main__":
    unittest.main()
