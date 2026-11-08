import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import sync_hub
from quota_sources import QuotaSnapshot
from usage_store import UsageStore, WeeklyLedger


def reading(provider="codex", used=40.0, at=1000.0, error=None):
    return QuotaSnapshot(provider, provider, 300, used, 100 - used, int(at) + 3600, at, False, error)


def record(payload, received=1000.0):
    return {"received_at": received, "payload": payload}


class FakeClient:
    def __init__(self, devices=None):
        self.devices = devices if devices is not None else {}
        self.puts: list[tuple[str, dict]] = []
        self.fail: Exception | None = None

    def put(self, device, payload):
        if self.fail:
            raise self.fail
        self.puts.append((device, payload))
        self.devices[device] = record(payload)

    def get_all(self):
        if self.fail:
            raise self.fail
        return dict(self.devices)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "sync.json"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in ("URL", "LINK_TOKEN", "TOKEN", "DEVICE"):
            os.environ.pop(f"CODEX_QUOTA_SYNC_{name}", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_no_file_means_sync_is_off(self):
        self.assertIsNone(sync_hub.load_config(self.path))

    def test_file_settings_and_environment_overrides(self):
        self.path.write_text(json.dumps({"url": "https://hub.example/", "token": "t", "device": "my pc"}), encoding="utf-8")
        config = sync_hub.load_config(self.path)
        self.assertEqual(config["url"], "https://hub.example")
        self.assertEqual(config["device"], "my-pc")
        os.environ["CODEX_QUOTA_SYNC_DEVICE"] = "mac-air"
        self.assertEqual(sync_hub.load_config(self.path)["device"], "mac-air")

    def test_a_token_is_required(self):
        self.path.write_text(json.dumps({"url": "https://hub.example"}), encoding="utf-8")
        self.assertIsNone(sync_hub.load_config(self.path))

    def test_device_ids_are_safe_for_the_hub(self):
        self.assertEqual(sync_hub.device_id("Mac Book/Pro!"), "Mac-Book-Pro")
        self.assertRegex(sync_hub.device_id(), r"^(win|mac)-[A-Za-z0-9_.-]+$")
        self.assertEqual(sync_hub.device_id("///"), "device")


class ClientTests(unittest.TestCase):
    def test_both_tokens_travel_in_headers_not_the_url(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            seen.append(request)
            return Response(b'{"devices": {"a": {}}}')

        client = sync_hub.HubClient({"url": "https://hub.example", "token": "app", "link_token": "gate"}, opener)
        client.put("win-pc", {"x": 1})
        self.assertEqual(client.get_all(), {"a": {}})
        put = seen[0]
        self.assertEqual(put.get_method(), "PUT")
        self.assertEqual(put.full_url, "https://hub.example/v1/devices/win-pc")
        self.assertEqual(put.get_header("X-sync-token"), "app")
        self.assertEqual(put.get_header("Authorization"), "Bearer gate")
        self.assertNotIn("app", put.full_url)
        self.assertEqual(json.loads(put.data), {"x": 1})


class SnapshotTests(unittest.TestCase):
    def test_only_real_readings_are_shared(self):
        shared = sync_hub.snapshot_payload({"codex": reading(), "claude": reading("claude", error="读取失败"),
                                            "grok": None, "mimo": reading("mimo", at=0)})
        self.assertEqual(set(shared), {"codex"})

    def test_a_reading_round_trips(self):
        original = reading()
        original.credits_balance = float("inf")
        back = sync_hub.snapshot_from(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(back.used_percent, 40.0)
        self.assertEqual(back.credits_balance, float("inf"))
        self.assertIsNone(sync_hub.snapshot_from({"provider": "codex"}))
        self.assertIsNone(sync_hub.snapshot_from("junk"))

    def test_only_newer_readings_from_other_devices_are_taken(self):
        payloads = {
            "me": record({"snapshots": {"codex": reading(at=5000).to_dict()}}),
            "mac": record({"snapshots": {"codex": reading(used=70, at=2000).to_dict(),
                                         "claude": reading("claude", at=500).to_dict(),
                                         "grok": reading("grok", at=9000).to_dict()}}),
        }
        current = {"codex": reading(at=1000), "claude": reading("claude", at=800)}
        accepted = []
        taken = sync_hub.merge_snapshots(payloads, "me", current, lambda p, s, src: accepted.append(src) or True)
        self.assertEqual([(p, s.used_percent) for p, s in taken], [("codex", 70)])   # grok is not enabled here
        self.assertEqual(accepted, ["sync:mac"])

    def test_a_reading_passed_on_does_not_come_back(self):
        mine = reading(at=3000)
        payloads = {"mac": record({"snapshots": {"codex": mine.to_dict()}})}
        taken = sync_hub.merge_snapshots(payloads, "me", {"codex": mine}, lambda *a: True)
        self.assertEqual(taken, [])


class LoopTests(unittest.TestCase):
    def make(self, client, merged=None):
        state = {"n": 0}

        def collect(now):
            return {"label": "PC", "sent_at": now, "snapshots": {}, "n": state["n"]}

        loop = sync_hub.SyncLoop(client, "win-pc", collect, lambda payloads, now: (merged if merged is not None else []).append(payloads))
        return loop, state

    def test_unchanged_payload_is_sent_again_only_as_a_heartbeat(self):
        client = FakeClient({"mac-air": record({"label": "Mac · air"})})
        merged = []
        loop, state = self.make(client, merged)
        self.assertTrue(loop.tick(0))
        self.assertFalse(loop.tick(60))                               # not due yet
        self.assertTrue(loop.tick(sync_hub.SYNC_EVERY))
        self.assertEqual(len(client.puts), 1)                         # unchanged: read only
        state["n"] = 1
        loop.tick(2 * sync_hub.SYNC_EVERY)
        self.assertEqual(len(client.puts), 2)
        loop.tick(2 * sync_hub.SYNC_EVERY + sync_hub.HEARTBEAT)
        self.assertEqual(len(client.puts), 3)
        self.assertEqual(len(merged), 4)
        self.assertIn("另有 1 台设备：Mac · air", loop.status(10))

    def test_failures_back_off_and_say_why(self):
        client = FakeClient()
        loop, _ = self.make(client)
        client.fail = urllib.error.HTTPError("u", 503, "x", None, None)
        self.assertFalse(loop.tick(0))
        self.assertEqual(loop.next_at, sync_hub.BACKOFF[0])
        self.assertFalse(loop.tick(sync_hub.BACKOFF[0]))
        self.assertEqual(loop.next_at, sync_hub.BACKOFF[0] + sync_hub.BACKOFF[1])
        self.assertIn("服务器暂停或冷却中", loop.status(sync_hub.BACKOFF[0]))
        client.fail = None
        self.assertTrue(loop.tick(loop.next_at))
        self.assertIn("已同步", loop.status())
        self.assertIn("暂无其他设备", loop.status())


class UsageMergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_other_devices_count_in_totals_but_are_never_exported(self):
        store = UsageStore(self.folder / "usage.json.gz")
        hour = 3600 * 1000
        store.hours = {"codex": {hour: [500, 2]}}
        store.set_remote("mac-air", "Mac · air", {"codex": {str(hour): [300, 1]}, "bad": "x"})
        self.assertEqual(store.hourly(["codex"], hour), {hour: 800})
        self.assertEqual(store.hourly(["codex"], hour, everywhere=False), {hour: 500})
        self.assertEqual(store.replies(["codex"], hour), 3)
        self.assertEqual(store.export_hours(hour), {"codex": {str(hour): [500, 2]}})
        self.assertEqual(store.by_device(["codex"], hour), [("本机", 500), ("Mac · air", 300)])
        store.keep_remote(["win-pc"])                                 # the hub dropped the Mac
        self.assertEqual(store.hourly(["codex"], hour), {hour: 500})

    def test_weeks_merge_keeping_the_larger_figures(self):
        ledger = WeeklyLedger(self.folder / "weekly.json")
        ledger.weeks = {"codex": [{"reset_at": 100000.0, "used": 60.0, "tokens": 1000}]}
        changed = ledger.merge({"codex": [{"reset_at": 100500.0, "used": 70.0, "tokens": 900},
                                          {"reset_at": 700000.0, "used": 20.0, "tokens": 10}],
                                "junk": "x"})
        self.assertTrue(changed)
        self.assertEqual(ledger.weeks["codex"], [{"reset_at": 100000.0, "used": 70.0, "tokens": 1000},
                                                 {"reset_at": 700000.0, "used": 20.0, "tokens": 10}])
        self.assertFalse(ledger.merge(ledger.export()))
        self.assertTrue((self.folder / "weekly.json").exists())


class AppMergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def payloads(self):
        hour = 3600 * 1000
        return {"mac-air": record({"label": "Mac · air", "snapshots": {"codex": reading(used=80, at=9000).to_dict()},
                                   "usage": {"codex": {str(hour): [300, 1]}},
                                   "weekly": {"codex": [{"reset_at": 100000.0, "used": 50.0, "tokens": 5}]}})}

    def test_the_tray_takes_newer_readings_hours_and_weeks(self):
        from tray_app import QuotaTray
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"codex": reading(at=1000), "claude": reading("claude", at=1000)}
        tray.history = None
        tray.usage = UsageStore(self.folder / "usage.json.gz")
        tray.weekly = WeeklyLedger(self.folder / "weekly.json")
        tray.sync = sync_hub.SyncLoop(FakeClient(), "win-pc", tray.sync_payload, tray.sync_merge)
        tray.update_ui = mock.Mock()
        tray.sync_merge(self.payloads(), 9500)
        self.assertEqual(tray.snapshots["codex"].used_percent, 80)
        self.assertEqual(tray.usage.by_device(["codex"], 0), [("本机", 0), ("Mac · air", 300)])
        self.assertEqual(tray.weekly.weeks["codex"][0]["used"], 50.0)
        tray.update_ui.assert_called_once()
        tray.sync_merge(self.payloads(), 9600)                       # the same round again: nothing new
        tray.update_ui.assert_called_once()
        payload = tray.sync_payload(9700)
        self.assertEqual(set(payload["snapshots"]), {"codex", "claude"})
        self.assertEqual(payload["usage"], {})                        # the Mac's hours are not passed on

    def test_the_mac_menu_takes_newer_readings_and_hours(self):
        import mac_menu
        with mock.patch.object(mac_menu, "load_cached_snapshots", return_value={}),                 mock.patch.object(mac_menu, "save_cached_snapshots") as saved:
            state = mac_menu.MenuState(keys=["codex"])
            state.snapshots["codex"] = reading(at=1000)
            state.monitor = mock.Mock(store=UsageStore(self.folder / "usage.json.gz"))
            state.sync = sync_hub.SyncLoop(FakeClient(), "mac-me", state.sync_payload, state.sync_merge)
            state.sync_merge(self.payloads(), 9500)
            self.assertEqual(state.snapshots["codex"].used_percent, 80)
            self.assertEqual(state.monitor.store.by_device(["codex"], 0)[1], ("Mac · air", 300))
            saved.assert_called_once()
            self.assertFalse(state.accept_snapshot("codex", reading(at=8000)))
            self.assertIn("codex", state.sync_payload(9600)["snapshots"])


class ServerTests(unittest.TestCase):
    """The hub itself, on a local port."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        (cls.folder / "token").write_text("secret\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SYNC_DIR": str(cls.folder / "devices"),
                                          "SYNC_TOKEN_FILE": str(cls.folder / "token")}):
            sys.modules.pop("sync_server", None)
            cls.server_module = importlib.import_module("sync_server")
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.server_module.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        sys.modules.pop("sync_server", None)
        shutil.rmtree(cls.folder, ignore_errors=True)

    def call(self, method, path, body=None, token="secret"):
        headers = {"X-Sync-Token": token} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_put_list_delete_and_auth(self):
        self.assertEqual(self.call("GET", "/v1/health", token=None)[0], 200)
        self.assertEqual(self.call("GET", "/v1/devices", token="wrong")[0], 401)
        self.assertEqual(self.call("PUT", "/v1/devices/bad%21name", {"a": 1})[0], 400)
        self.assertEqual(self.call("PUT", "/v1/devices/win-pc", [1])[0], 400)
        self.assertEqual(self.call("PUT", "/v1/devices/win-pc", {"label": "PC"})[0], 200)
        status, body = self.call("GET", "/v1/devices")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"]["win-pc"]["payload"], {"label": "PC"})
        self.assertEqual(self.call("DELETE", "/v1/devices/win-pc")[0], 200)
        self.assertNotIn("win-pc", self.call("GET", "/v1/devices")[1]["devices"])

    def test_the_client_talks_to_the_hub(self):
        client = sync_hub.HubClient({"url": self.url, "token": "secret"})
        client.put("mac-air", {"label": "Mac"})
        self.assertEqual(client.get_all()["mac-air"]["payload"], {"label": "Mac"})


if __name__ == "__main__":
    unittest.main()
