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
            os.environ.pop(f"CIRCLOW_SYNC_{name}", None)

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
        os.environ["CIRCLOW_SYNC_DEVICE"] = "mac-air"
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

    def test_live_tps_goes_through_memory_only(self):
        self.assertEqual(self.call("PUT", "/v1/live/win-pc", {"providers": {}}, token="wrong")[0], 401)
        self.assertEqual(self.call("PUT", "/v1/live/bad%21name", {"a": 1})[0], 400)
        client = sync_hub.HubClient({"url": self.url, "token": "secret"})
        client.put_live("win-pc", {"label": "PC", "providers": {"codex": {"tps": 80}}})
        live, hub_now = client.get_live()
        self.assertEqual(live["win-pc"]["payload"]["providers"]["codex"]["tps"], 80)
        self.assertLess(abs(hub_now - time.time()), 5)
        self.assertFalse(any(self.folder.rglob("*win-pc*")))           # nothing written to disk
        self.server_module.LIVE["old"] = {"received_at": time.time() - 3600, "payload": {}}
        self.assertNotIn("old", client.get_live()[0])


def perf(providers, total=None):
    rows = {}
    for provider, (tps, sample_at, active, tokens) in providers.items():
        rows[provider] = {"label": provider, "display_tps": tps, "generation_tps": tps, "display_model": f"{provider}-model",
                          "last_sample_at": sample_at, "active_sessions": active, "output_tokens_60": tokens,
                          "output_tps_60": round(tokens / 60, 2)}
    return {"providers": rows, "total_output_tps_60": total if total is not None else round(sum(r["output_tokens_60"] for r in rows.values()) / 60, 2)}


class LiveTpsTests(unittest.TestCase):
    NOW = 10_000.0

    def remote(self, providers, late=1.0, hub_now=50_000.0, label="Mac · air"):
        """A record as the hub returns it: ages measured on the Mac, hub clock unrelated to ours."""
        payload = sync_hub.live_payload(perf(providers), 7_000.0, label)
        return {"mac-air": {"received_at": hub_now - late, "payload": payload}}, hub_now

    def test_the_payload_carries_ages_not_timestamps(self):
        payload = sync_hub.live_payload(perf({"codex": (90.0, 9_990.0, 1, 600), "grok": (None, None, 0, 0)}), self.NOW, "PC")
        self.assertEqual(payload["providers"], {"codex": {"active": 1, "tps": 90.0, "age": 10.0, "model": "codex-model", "tokens_60": 600}})

    def test_every_device_shows_the_total_tps_and_the_summed_output(self):
        local = perf({"codex": (60.0, self.NOW - 40, 1, 1200), "claude": (30.0, self.NOW - 20, 0, 0)})
        live, hub_now = self.remote({"codex": (95.0, 7_000.0 - 5, 2, 600)}, late=2.0)
        merged = sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)
        codex = merged["providers"]["codex"]
        self.assertEqual(codex["display_tps"], 155.0)                   # both generating: 60 + 95
        self.assertEqual(codex["generation_tps"], 155.0)
        self.assertEqual(codex["display_model"], "codex-model")
        self.assertAlmostEqual(codex["last_sample_at"], self.NOW - 7)
        self.assertEqual((codex["active_sessions"], codex["output_tokens_60"], codex["output_tps_60"]), (3, 1800, 30.0))
        self.assertEqual((codex["local_tps"], codex["local_active"], codex["local_output_tps_60"]), (60.0, 1, 20.0))
        self.assertEqual(codex["devices"], [{"label": "Mac · air", "tps": 95.0, "active": 2}])
        self.assertEqual(merged["providers"]["claude"]["display_tps"], 30.0)
        self.assertEqual(merged["providers"]["claude"]["devices"], [])
        self.assertEqual((merged["total_output_tps_60"], merged["local_total_output_tps_60"]), (30.0, 20.0))
        self.assertEqual(local["providers"]["codex"]["display_tps"], 60.0)      # the input is left alone

    def test_only_devices_generating_now_add_up(self):
        # The Mac finished a reply 7 s ago and is idle; this PC is generating: 60, not 155.
        local = perf({"codex": (60.0, self.NOW - 40, 1, 1200)})
        live, hub_now = self.remote({"codex": (95.0, 7_000.0 - 5, 0, 0)}, late=2.0)
        codex = sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]
        self.assertEqual((codex["display_tps"], codex["local_tps"]), (60.0, 60.0))
        # Nobody generating: the most recent reply's speed, wherever it ran.
        local = perf({"codex": (60.0, self.NOW - 40, 0, 0)})
        codex = sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]
        self.assertEqual(codex["display_tps"], 95.0)
        # A working device whose last reply is too old to show adds nothing.
        local = perf({"codex": (60.0, self.NOW - sync_hub.SAMPLE_AGE - 1, 1, 0)})
        live, hub_now = self.remote({"codex": (95.0, 7_000.0 - 5, 1, 600)}, late=2.0)
        codex = sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]
        self.assertEqual(codex["display_tps"], 95.0)
        # Three devices at once.
        live["mac-mini"] = {"received_at": hub_now - 1, "payload": sync_hub.live_payload(
            perf({"codex": (40.5, 7_000.0 - 3, 1, 300)}), 7_000.0, "Mac · mini")}
        local = perf({"codex": (60.0, self.NOW - 2, 1, 1200)})
        codex = sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]
        self.assertEqual(codex["display_tps"], 195.5)
        self.assertEqual(codex["local_tps"], 60.0)

    def test_both_devices_agree(self):
        """Windows folds in the Mac and the Mac folds in Windows: the same figures."""
        windows = perf({"codex": (60.0, self.NOW - 40, 1, 1200)})
        mac_now = 7_000.0
        mac = perf({"codex": (95.0, mac_now - 5, 2, 600)})
        hub_now = 50_000.0
        live = {"win-pc": {"received_at": hub_now, "payload": sync_hub.live_payload(windows, self.NOW, "PC")},
                "mac-air": {"received_at": hub_now, "payload": sync_hub.live_payload(mac, mac_now, "Mac")}}
        on_windows = sync_hub.combine(windows, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]
        on_mac = sync_hub.combine(mac, live, hub_now, "mac-air", mac_now)["providers"]["codex"]
        for key in ("display_tps", "active_sessions", "output_tps_60"):
            self.assertEqual(on_windows[key], on_mac[key], key)
        self.assertEqual((on_windows["local_tps"], on_mac["local_tps"]), (60.0, 95.0))

    def test_a_silent_device_or_an_old_reply_no_longer_counts(self):
        local = perf({"codex": (60.0, self.NOW - 40, 0, 0)})
        live, hub_now = self.remote({"codex": (95.0, 7_000.0 - 5, 1, 600)}, late=sync_hub.LIVE_FRESH + 5)
        self.assertEqual(sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]["display_tps"], 60.0)
        live, hub_now = self.remote({"codex": (95.0, 7_000.0 - sync_hub.SAMPLE_AGE + 1, 0, 0)}, late=10)
        self.assertEqual(sync_hub.combine(local, live, hub_now, "win-pc", self.NOW)["providers"]["codex"]["display_tps"], 60.0)

    def test_without_other_devices_only_the_local_fields_are_added(self):
        local = perf({"codex": (60.0, self.NOW - 4, 1, 300)})
        merged = sync_hub.combine(local, {}, self.NOW, "win-pc", self.NOW)["providers"]["codex"]
        self.assertEqual((merged["display_tps"], merged["local_tps"], merged["devices"]), (60.0, 60.0, []))

    def test_the_live_loop_speeds_up_while_anyone_works(self):
        client = mock.Mock()
        client.get_live.return_value = ({}, 1000.0)
        payloads = {"value": {"providers": {}}}
        loop = sync_hub.LiveLoop(client, "win-pc", lambda now: payloads["value"])
        self.assertTrue(loop.tick(0))
        self.assertEqual(loop.next_at, sync_hub.LIVE_IDLE)
        payloads["value"] = {"providers": {"codex": {"active": 1}}}
        loop.wake()
        self.assertTrue(loop.tick(5))
        self.assertEqual(loop.next_at, 5 + sync_hub.LIVE_ACTIVE)
        payloads["value"] = {"providers": {}}
        client.get_live.return_value = ({"mac-air": {"received_at": 999.0, "payload": {"providers": {"codex": {"active": 2}}}}}, 1000.0)
        loop.tick(loop.next_at)
        self.assertEqual(loop.next_at - 8, sync_hub.LIVE_ACTIVE)                 # the Mac is working
        client.put_live.side_effect = OSError("down")
        self.assertFalse(loop.tick(loop.next_at))
        live, hub_now = loop.view(20)
        self.assertIn("mac-air", live)
        self.assertEqual(hub_now, 1000.0 + 12)

    def test_the_tray_shows_the_shared_figure_and_its_own(self):
        from tray_app import QuotaTray
        tray = QuotaTray.__new__(QuotaTray)
        self.assertIsNone(QuotaTray.local_tps("codex", {"display_tps": 60.0}))     # no sync: no "本机" line
        self.assertEqual(QuotaTray.local_tps("codex", {"local_tps": 60.0}), 60.0)
        self.assertIsNone(QuotaTray.local_tps("gemini", {"local_tps": 60.0}))
        tray.live = mock.Mock(device="win-pc")
        tray.live.view.return_value = self.remote({"codex": (95.0, 7_000.0 - 5, 2, 600)})
        merged = tray.with_other_devices(perf({"codex": (60.0, self.NOW - 40, 1, 1200)}), self.NOW)
        self.assertEqual(merged["providers"]["codex"]["display_tps"], 155.0)       # both generating: the total
        tray.live = None
        local = perf({"codex": (60.0, self.NOW - 40, 1, 1200)})
        self.assertIs(tray.with_other_devices(local, self.NOW), local)


if __name__ == "__main__":
    unittest.main()
