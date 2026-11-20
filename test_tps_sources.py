import json
import sqlite3
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tps_sources import JsonlReader, Observation, TpsMonitor, key, logged_model, proto_fields


def vint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def pv(tag, value):
    return vint(tag * 8) + vint(value)


def pb(tag, value):
    return vint(tag * 8 + 2) + vint(len(value)) + value


def stamp(value):
    whole = int(value)
    return pv(1, whole) + pv(2, round((value - whole) * 1e9))


class TpsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(__file__).parent / "work/tps-tests" / str(uuid.uuid4())
        self.home.mkdir(parents=True)
        self.now = time.time()
        self.monitor = TpsMonitor(home=self.home)
        self.monitor.started_at = self.now
        self.monitor.context["s"] = {"model": "gpt-6.1-sol", "source": "Codex"}

    def event(self, response="r", output=120, when=None, thread="s"):
        return {"timestamp": self.now - 1 if when is None else when, "type": "token_usage_record", "payload": {"thread_id": thread, "response_id": response, "usage": {"output_tokens": output, "input_tokens": 99999, "cached_input_tokens": 99999, "reasoning_output_tokens": 100}}}

    def test_counts_output_once_and_does_not_add_input_cache_or_reasoning(self):
        event = self.event()
        self.monitor._codex_record(event, "s", self.now)
        self.monitor._codex_record(event, "s", self.now)
        self.monitor._codex_record({"timestamp": self.now, "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 120}, "total_token_usage": {"output_tokens": 120}}}}, "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["output_tokens_60"], 120)
        self.assertEqual(data["output_tps_60"], 2)

    def test_codex_rate_limits_from_token_count_are_kept_newest_first(self):
        limits = {"limit_id": "codex", "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_in_seconds": 600},
                  "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": int(self.now) + 9000}}
        self.monitor._codex_record({"timestamp": self.now - 2, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}, "s", self.now)
        older = dict(limits, primary={"used_percent": 10.0, "window_minutes": 300})
        self.monitor._codex_record({"timestamp": self.now - 50, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": older}}, "s", self.now)
        other = dict(limits, limit_id="codex_other")
        self.monitor._codex_record({"timestamp": self.now, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": other}}, "s", self.now)
        moment, kept = self.monitor.codex_rate_limits
        self.assertAlmostEqual(moment, self.now - 2, places=3)
        self.assertEqual(kept["primary"]["used_percent"], 100.0)
        from quota_sources import parse_codex_session_limits
        snapshot = parse_codex_session_limits(kept, moment)
        self.assertEqual(snapshot.remaining_percent, 0.0)
        self.assertEqual(snapshot.resets_at, int(moment + 600))
        self.assertEqual(snapshot.secondary_remaining_percent, 60.0)

    def test_codex_usage_limit_error_is_noticed(self):
        self.monitor._codex_record({"timestamp": self.now - 1, "type": "event_msg", "payload": {"type": "error", "message": "You've hit your usage limit. Try again later."}}, "s", self.now)
        self.assertAlmostEqual(self.monitor.codex_limit_hit_at, self.now - 1, places=3)
        self.monitor.context["m"] = {"model": "mimo-v2.5-pro"}
        self.monitor._codex_record({"timestamp": self.now, "type": "event_msg", "payload": {"type": "error", "message": "rate limit"}}, "m", self.now)
        self.assertAlmostEqual(self.monitor.codex_limit_hit_at, self.now - 1, places=3)

    def test_fork_replay_is_not_new_generation(self):
        self.monitor.context["fork"] = {"model": "grok-4.7", "source": "Codex"}
        self.monitor._codex_record(self.event(thread="s"), "fork", self.now)
        self.assertFalse(self.monitor.events)

    def test_request_duration_is_not_presented_as_client_native_tps(self):
        self.monitor._codex_record({"timestamp":self.now-11,"type":"event_msg","payload":{"type":"task_started"}}, "s", self.now)
        self.monitor._codex_record(self.event(output=100), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["basis"], ["客户端暂未提供可核验TPS"])

    def test_missing_duration_stays_unknown_and_sixty_second_rate_remains_valid(self):
        self.monitor._codex_record(self.event(), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["display_basis"], "客户端暂未取得TPS")
        self.assertIsNone(data["display_tps"])

    def test_old_completion_does_not_clear_new_inflight_request(self):
        self.monitor.activity["s"] = ("codex", self.now, "请求中")
        self.monitor._codex_record(self.event(when=self.now - 10), "s", self.now)
        self.monitor._codex_record({"timestamp": self.now - 5, "type": "event_msg", "payload": {"type": "task_complete"}}, "s", self.now)
        self.assertIn("s", self.monitor.activity)

    def test_finished_generation_recovers_idle_after_three_seconds(self):
        self.monitor.activity["s"] = ("codex", self.now - 1, "刚完成生成")
        self.monitor.last_discovery = self.now
        self.assertEqual(self.monitor.poll(self.now + 1)["providers"]["codex"]["active_sessions"], 1)
        self.assertEqual(self.monitor.poll(self.now + 4)["providers"]["codex"]["active_sessions"], 0)

    def test_window_counts_all_sessions_and_expires_without_interpolating(self):
        self.monitor.add(Observation("1", "Codex", "a", "codex", "gpt", self.now - 59, 60), self.now)
        self.monitor.add(Observation("2", "Codex", "b", "codex", "gpt", self.now - 1, 120), self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["total_output_tps_60"], 3)
        self.assertEqual(self.monitor.snapshot(self.now + 3)["total_output_tps_60"], 2)

    def test_grok_elapsed_time_does_not_replace_native_tps(self):
        record = {"timestamp": self.now - 1, "params": {"update": {"sessionUpdate": "turn_completed", "prompt_id": "p", "elapsed_ms": 10000, "usage": {"modelUsage": {"grok-4.7": {"outputTokens": 200, "reasoningTokens": 100, "apiDurationMs": 2000}}}}}}
        self.monitor._grok_record(record, "g", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["grok"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["output_tokens_60"], 200)

    def test_grok_future_turn_completion_does_not_double_count_counter(self):
        folder = self.home / "grok/g"
        folder.mkdir(parents=True)
        path = folder / "updates.jsonl"
        path.write_text("", encoding="utf-8")
        usage = {"updatedAt": self.now, "session": {"modelUsage": {"grok-4.7": {"outputTokens": 1000}}}}
        folder.joinpath("usage.json").write_text(json.dumps(usage))
        self.monitor._grok_usage_counter(path, self.now)
        usage["updatedAt"] = self.now + 1
        usage["session"]["modelUsage"]["grok-4.7"]["outputTokens"] = 1100
        folder.joinpath("usage.json").write_text(json.dumps(usage))
        self.monitor._grok_usage_counter(path, self.now + 1)
        record = {"timestamp": self.now + 1, "params": {"update": {"sessionUpdate": "turn_completed", "prompt_id": "p", "usage": {"modelUsage": {"grok-4.7": {"outputTokens": 100, "apiDurationMs": 1000}}}}}}
        self.monitor._grok_record(record, "g", self.now + 1)
        self.assertEqual(self.monitor.snapshot(self.now + 1)["providers"]["grok"]["output_tokens_60"], 100)

    def test_grok_first_token_detects_activity_without_waiting_for_turn_end(self):
        self.monitor._grok_activity({"type": "first_token", "ts": self.now}, "g", self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["grok"]["active_sessions"], 1)

    def test_isolated_gemini_bridge_copy_is_not_counted_again(self):
        self.monitor.add(Observation("native", "Antigravity CLI · Codex隔离配置", "a", "gemini", "gemini", self.now - 2, 200, 2, "流式"), self.now)
        self.monitor.add(Observation("copy", "Codex · Gemini桥接回传", "b", "gemini", "gemini", self.now - 1, 200, 4, "API平均"), self.now)
        data = self.monitor.snapshot(self.now)["providers"]["gemini"]
        self.assertEqual(data["output_tokens_60"], 200)
        self.assertIsNone(data["generation_tps"])

    def make_agy_db(self, corrupt_total=False, step_type=None, total=True):
        folder = self.home / ".gemini/antigravity-cli/conversations"
        folder.mkdir(parents=True)
        path = folder / "a.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY,data BLOB)")
        if step_type is None:
            db.execute("CREATE TABLE steps(idx INTEGER,status INTEGER,metadata BLOB)")
        else:
            db.execute("CREATE TABLE steps(idx INTEGER,step_type INTEGER,status INTEGER,metadata BLOB)")
        usage = (pv(3, 51 if corrupt_total else 50) if total else b"") + pv(9, 40) + pv(10, 10) + pb(11, b"response-a")
        model = pb(4, usage) + pb(11, stamp(20)) + pb(12, stamp(2)) + pb(19, b"gemini-3.8-flash")
        db.execute("INSERT INTO gen_metadata VALUES(0,?)", (pb(1, model),))
        metadata = pb(1, stamp(self.now - 3)) + pb(7, stamp(self.now - 1)) + pb(9, pb(11, b"response-a"))
        if step_type is None:
            db.execute("INSERT INTO steps VALUES(0,3,?)", (metadata,))
        else:
            db.execute("INSERT INTO steps VALUES(0,?,3,?)", (step_type, metadata))
        db.commit()
        db.close()
        return path

    def test_antigravity_reply_step_time_gives_tps(self):
        self.make_agy_db(step_type=15)
        self.monitor._antigravity(self.now)
        data = self.monitor.snapshot(self.now)["providers"]["gemini"]
        self.assertEqual(data["generation_tps"], 25.0)          # 50 tokens / 2 s
        self.assertIn("含首字延迟", data["display_basis"])

    def test_antigravity_other_step_types_do_not_give_tps(self):
        self.make_agy_db(step_type=7)
        self.monitor._antigravity(self.now)
        self.assertIsNone(self.monitor.snapshot(self.now)["providers"]["gemini"]["generation_tps"])

    def test_antigravity_without_total_field_still_counts(self):
        self.make_agy_db(total=False)
        self.monitor._antigravity(self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["gemini"]["output_tokens_60"], 50)

    def test_antigravity_locked_database_is_read_from_a_copy(self):
        path = self.make_agy_db(step_type=15)
        holder = sqlite3.connect(path)
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            self.monitor._antigravity(self.now)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["gemini"]["generation_tps"], 25.0)

    def test_antigravity_diagnostics_hide_text(self):
        from tps_sources import antigravity_diagnostics
        self.make_agy_db(step_type=15)
        report = antigravity_diagnostics(home=self.home)
        text = json.dumps(report)
        self.assertNotIn("response-a", text)
        newest = next(root for root in report["roots"] if root["databases"])["newest"][0]
        self.assertEqual(newest["steps"][0]["step_type"], 15)
        self.assertEqual(newest["gen_metadata"][0]["chat_model"]["19"], "gemini-3.8-flash")

    def test_antigravity_conversation_gets_its_request_and_title(self):
        path = self.make_agy_db(step_type=15)
        db = sqlite3.connect(path)
        db.execute("ALTER TABLE steps ADD COLUMN step BLOB")
        prompt = "帮我把 Gemini 的额度池拆成三个并更新托盘".encode()
        body = pb(2, pb(1, b"9f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f") + pb(3, pb(1, prompt)))
        db.execute("INSERT INTO steps VALUES(1,14,3,?,?)", (pb(1, stamp(self.now - 5)), body))
        db.commit()
        db.close()
        brain = self.home / ".gemini/antigravity/brain/a"
        brain.mkdir(parents=True)
        (brain / "task.md").write_text("# Task: 拆分 Gemini 额度池\n- [ ] step", encoding="utf-8")
        self.monitor._antigravity(self.now)
        note = self.monitor.notes.notes["a"]
        self.assertEqual(note.request, prompt.decode())
        self.assertEqual(note.title, "拆分 Gemini 额度池")
        agents = self.monitor.agents(self.now)
        agent = next(item for item in agents if item["key"] == "a")
        self.assertEqual(agent["provider"], "gemini")
        self.assertEqual(agent["source"], "Antigravity CLI")
        self.assertIn("额度池", agent["title"])

    def test_antigravity_request_skips_ids_and_code(self):
        from tps_sources import antigravity_request
        body = pb(1, b"0123456789abcdef0123456789abcdef") + pb(2, b"def f(x): return {(x)}; y = (1); z = [2]; {a}") + pb(3, b"fix the login flow please")
        self.assertEqual(antigravity_request(body), "fix the login flow please")
        self.assertEqual(antigravity_request(b""), "")

    def test_antigravity_native_step_payload_and_running_tool_with_provisional_end(self):
        path = self.make_agy_db(step_type=15)
        with sqlite3.connect(path) as db:
            db.execute("ALTER TABLE steps ADD COLUMN step_payload BLOB")
            prompt = "保留额度耗尽预测并完成设置页面"
            metadata = pb(1, stamp(self.now - 4))
            db.execute("INSERT INTO steps VALUES(1,14,3,?,?)", (metadata, pb(3, pb(1, prompt.encode()))))
            tool = pb(2, b"replace_file_content") + pb(3, json.dumps({"TargetFile": "D:/repo/agent_panel.py"}).encode())
            metadata = pb(1, stamp(self.now - 1)) + pb(7, stamp(self.now)) + pb(20, pv(3, 1)) + pb(4, tool)
            db.execute("INSERT INTO steps VALUES(2,8,2,?,NULL)", (metadata,))
        self.monitor._antigravity(self.now)
        note = self.monitor.notes.notes["a"]
        self.assertEqual(note.request, prompt)
        self.assertEqual(note.action, "编辑 agent_panel.py")
        self.assertIn("a", self.monitor.activity)

    def test_antigravity_preserves_counts_without_estimating_tps(self):
        self.make_agy_db()
        self.monitor._antigravity(self.now)
        data = self.monitor.snapshot(self.now)["providers"]["gemini"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["output_tokens_60"], 50)

    def test_client_native_tps_is_copied_exactly_and_latest_value_wins(self):
        self.monitor.add(Observation("native-1", "Grok CLI 原生统计", "g", "grok", "grok", self.now - 2, 200, None, "原生", False, 110.4), self.now)
        self.monitor.add(Observation("native-2", "Grok CLI 原生统计", "g", "grok", "grok", self.now - 1, 250, None, "原生", False, 124.4), self.now)
        data = self.monitor.snapshot(self.now)["providers"]["grok"]
        self.assertEqual(data["generation_tps"], 124.4)
        self.assertEqual(data["output_tokens_60"], 0)

    def test_codex_stream_uses_real_output_count_and_client_item_time(self):
        self.monitor.client_stream_starts["s"] = [self.now - 3, self.now - 2, self.now - 1]
        self.monitor._codex_record(self.event(output=120, when=self.now), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], 40)
        self.assertIn("真实输出token", data["basis"][0])

    def test_codex_stream_ends_at_last_finished_output_item(self):
        self.monitor.client_stream_starts["s"] = [self.now - 4]
        self.monitor.client_stream_ends["s"] = [self.now - 3, self.now - 2]   # record written 2 s later
        self.monitor._codex_record(self.event(output=120, when=self.now), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], 60)
        self.assertIn("最后输出项完成", data["basis"][0])

    def test_codex_tiny_reply_gives_no_tps(self):
        self.monitor.client_stream_starts["s"] = [self.now - 2]
        self.monitor._codex_record(self.event(output=5, when=self.now), "s", self.now)
        self.assertIsNone(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"])

    def test_recent_replies_are_token_weighted(self):
        self.monitor.add(Observation("a", "Codex", "s", "codex", "gpt", self.now - 30, 1000, 10.0, "流式", True, 100.0), self.now)
        self.monitor.add(Observation("b", "Codex", "s", "codex", "gpt", self.now - 1, 20, 1.0, "流式", True, 20.0), self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], round(1020 / 11, 1))
        self.assertIn("加权", data["display_basis"])
        self.monitor.add(Observation("c", "Codex", "s", "codex", "gpt", self.now - 200, 1000, 1.0, "流式", True, 1000.0), self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"], round(1020 / 11, 1))

    def test_codex_total_output_includes_tool_arguments_and_is_labelled(self):
        self.monitor.client_stream_starts["s"] = [self.now - 3, self.now - 1]
        self.monitor._codex_record({"timestamp":self.now-1,"type":"response_item","payload":{"type":"custom_tool_call","input":"PRIVATE_CANARY"}}, "s", self.now)
        self.monitor._codex_record(self.event(output=120, when=self.now), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], 40)
        self.assertIn("工具调用输出", data["basis"][0])

    def test_codex_client_logs_keep_concurrent_sessions_separate(self):
        self.monitor.codex_root.mkdir(parents=True)
        db = sqlite3.connect(self.monitor.codex_root / "logs_2.sqlite")
        db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
        for i, (session, age) in enumerate((("s", 3), ("other", 1), (None, 5)), 1):
            moment = self.now - age
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?)", (i, int(moment), round((moment-int(moment))*1e9), "codex_core::stream_events_utils", 'Output item item_type="reasoning" item_id="PRIVATE_ID"', session))
        db.commit()
        db.close()
        self.monitor._codex_logs(self.now)
        self.assertEqual(set(self.monitor.client_stream_starts), {"s", "other"})
        self.monitor._codex_record(self.event(when=self.now), "s", self.now)
        self.assertAlmostEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"], 40)

    def test_stream_span_model_keeps_unlisted_gpt_session_on_codex_icon(self):
        self.monitor.codex_root.mkdir(parents=True)
        db = sqlite3.connect(self.monitor.codex_root / "logs_2.sqlite")
        db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
        moment = self.now - 1
        body = 'turn{model=gpt-6-luna}: Output item item_type="reasoning" item_id="item"'
        db.execute(
            "INSERT INTO logs VALUES(?,?,?,?,?,?)",
            (1, int(moment), 0, "codex_core::stream_events_utils", body, "missing-thread"),
        )
        db.commit()
        db.close()
        self.assertEqual(logged_model(body), "gpt-6-luna")
        self.monitor._codex_logs(self.now)
        self.assertEqual(self.monitor.activity["missing-thread"][0], "codex")
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["active_sessions"], 1)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["other"]["active_sessions"], 0)

    def test_codex_next_response_excludes_prior_stream_start(self):
        self.monitor.completed_at["s"] = self.now - 2
        self.monitor.client_stream_starts["s"] = [self.now - 10, self.now - 1]
        self.monitor._codex_record(self.event(when=self.now), "s", self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"], 120)

    def test_antigravity_mismatched_proto_totals_are_skipped(self):
        self.make_agy_db(corrupt_total=True)
        self.monitor._antigravity(self.now)
        self.assertFalse(self.monitor.events)

    def test_malformed_proto_is_rejected(self):
        with self.assertRaises(ValueError):
            proto_fields(b"\x0a\xff\xff")

    def test_jsonl_partial_line_is_counted_once_when_finished(self):
        path = self.home / "a.jsonl"
        path.write_bytes(b'{"x":1}\n{"x":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"x": 1}])
        with path.open("ab") as output:
            output.write(b'2}\n')
        self.assertEqual(list(reader.read()), [{"x": 2}])
        self.assertFalse(list(reader.read()))

    def test_jsonl_idle_partial_record_does_not_reopen_and_resumes_on_append(self):
        path = self.home / "idle.jsonl"
        path.write_bytes(b'{"x":1}\n{"x":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"x": 1}])
        with patch.object(Path, "open", side_effect=AssertionError("idle file reopened")):
            self.assertEqual(list(reader.read()), [])
            self.assertEqual(list(reader.read()), [])
        with path.open("ab") as output:
            output.write(b'2}\n')
        self.assertEqual(list(reader.read()), [{"x": 2}])

    def test_jsonl_truncation_to_empty_discards_partial_before_append(self):
        path = self.home / "truncated.jsonl"
        path.write_bytes(b'{"old":1}\n{"old":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"old": 1}])
        path.write_bytes(b'')
        self.assertEqual(list(reader.read()), [])
        path.write_bytes(b'{"new":2}\n')
        self.assertEqual(list(reader.read()), [{"new": 2}])

    def test_grok_usage_json_is_parsed_only_after_it_changes(self):
        folder = self.home / "grok/u"
        folder.mkdir(parents=True)
        path = folder / "updates.jsonl"
        path.write_text("", encoding="utf-8")
        usage = folder / "usage.json"
        usage.write_text("{}")
        self.assertTrue(self.monitor._usage_changed(path))
        self.assertFalse(self.monitor._usage_changed(path))
        usage.write_text('{"session": {}}')
        self.assertTrue(self.monitor._usage_changed(path))

    def test_old_histories_are_not_registered_and_idle_readers_are_released(self):
        import os
        old = self.home / "old.jsonl"
        old.write_text("{}\n")
        os.utime(old, (self.now - 3 * 86400, self.now - 3 * 86400))
        self.assertFalse(self.monitor._recent(old, self.now))
        fresh = self.home / "fresh.jsonl"
        fresh.write_text("{}\n")
        self.assertTrue(self.monitor._recent(fresh, self.now))
        self.monitor._register(old, "old", "Codex")
        self.monitor._register(fresh, "fresh", "Codex")
        self.monitor.client_stream_starts["gone"] = [self.now - 3600]
        self.monitor._forget_idle(self.now)
        self.assertNotIn(old, self.monitor.readers)
        self.assertIn(fresh, self.monitor.readers)
        self.assertNotIn("gone", self.monitor.client_stream_starts)
        self.assertNotIn("old", self.monitor.context)
        self.assertIn("fresh", self.monitor.context)

    def test_process_scan_is_throttled(self):
        with patch("tps_sources.running_software", return_value=[]) as scan:
            self.monitor.discover(self.now)
            self.monitor.discover(self.now + 2)
            self.monitor.discover(self.now + 31)
        self.assertEqual(scan.call_count, 2)

    def test_old_events_keep_their_source_but_not_rates_and_session_maps_are_pruned(self):
        from tps_sources import Observation, FORGET_SECONDS
        self.monitor.add(Observation("old", "Claude Code", "a", "claude", "m", self.now - 7200, 500, 10.0, "b", True, 50.0), self.now)
        self.monitor.add(Observation("new", "Codex", "s", "codex", "m", self.now - 5, 300, 3.0, "b", True, 100.0), self.now)
        data = self.monitor.snapshot(self.now)["providers"]
        self.assertEqual(["Claude Code"], data["claude"]["sources"])
        self.assertEqual(0, data["claude"]["output_tokens_60"])
        self.assertIsNone(data["claude"]["generation_tps"])
        self.assertEqual(300, data["codex"]["output_tokens_60"])
        self.assertEqual(100.0, data["codex"]["generation_tps"])
        self.assertEqual(2, self.monitor.added_total)
        self.monitor.claude_last_input["gone"] = self.now - FORGET_SECONDS - 10
        self.monitor.grok_counters[("gone", "grok")] = 5
        self.monitor._forget_idle(self.now)
        self.assertNotIn("gone", self.monitor.claude_last_input)
        self.assertNotIn(("gone", "grok"), self.monitor.grok_counters)

    def test_snapshot_does_not_persist_private_message_or_request_id(self):
        value = self.event(response="private-response-id")
        value["payload"]["response"] = "PRIVATE_PROMPT_CANARY"
        self.monitor._codex_record(value, "s", self.now)
        output = json.dumps(self.monitor.snapshot(self.now))
        self.assertNotIn("PRIVATE_PROMPT_CANARY", output)
        self.assertNotIn("private-response-id", output)
        self.assertNotIn("PRIVATE_PROMPT_CANARY", key("PRIVATE_PROMPT_CANARY"))

    def test_tps_is_held_while_the_client_keeps_working(self):
        import tempfile
        from tps_sources import Observation
        monitor = TpsMonitor(Path(tempfile.mkdtemp()))
        now = 10_000.0
        monitor.add(Observation("r1", "Antigravity CLI", "conv", "gemini", "gemini-3", now - 900, 400, 8.0, "本地步骤时间", True, 50.0), now)
        self.assertIsNone(monitor.snapshot(now)["providers"]["gemini"]["display_tps"])      # idle: no stale rate
        monitor.activity["conv"] = ("gemini", now - 5, "请求中")
        self.assertEqual(50.0, monitor.snapshot(now)["providers"]["gemini"]["display_tps"])  # working: last rate, not 0


class ClaudeStreamTests(unittest.TestCase):
    """A Claude Code session and its sub-agents write their replies at once."""

    def setUp(self):
        self.now = time.time()
        self.monitor = TpsMonitor(home=Path(__file__).parent / "work/tps-tests" / str(uuid.uuid4()))

    def request(self, stream, ident, at):
        self.monitor._claude_record({"type": "api-request", "id": ident, "timestamp": at,
                                     "params": {"model": "claude-opus-5-5"}}, "s", self.now, stream)

    def line(self, stream, mid, ref, at, output, stop=None):
        message = {"id": mid, "model": "claude-opus-5-5", "stop_reason": stop, "usage": {"output_tokens": output}}
        self.monitor._claude_record({"type": "assistant", "requestRef": ref, "timestamp": at, "message": message},
                                    "s", self.now, stream)

    def test_sub_agent_lines_do_not_settle_the_sessions_reply(self):
        t = self.now - 60
        self.request("s", "r1", t)
        self.request("agent-a", "r2", t + 1)
        self.line("s", "m1", "r1", t + 5, 40)                    # thinking block: count so far
        self.line("agent-a", "m2", "r2", t + 6, 300)             # the sub-agent writes in between
        self.line("s", "m1", "r1", t + 10, 1000, "tool_use")      # the session's reply goes on
        self.line("agent-a", "m2", "r2", t + 11, 500, "end_turn")
        self.monitor._claude_flush(self.now)
        self.monitor._claude_record({"type": "user", "timestamp": t + 12, "message": {}}, "s", self.now, "s")
        events = {e.identity: e for e in self.monitor.events}
        session, agent = events[key("claude", "m1")], events[key("claude", "m2")]
        self.assertEqual((session.output_tokens, session.native_tps, session.stream), (1000, 100.0, ""))
        self.assertEqual((agent.output_tokens, agent.native_tps, agent.stream, agent.turn_end), (500, 50.0, "agent-a", True))
        self.assertEqual(len(self.monitor.events), 2)

    def observe(self, ident, stream, ended, tokens, seconds, session="s", turn_end=False):
        self.monitor.add(Observation(ident, "Claude Code", session, "claude", "claude-opus-5-5", ended, tokens, seconds,
                                     "b", True, round(tokens / seconds, 1), stream=stream, turn_end=turn_end), self.now)

    def test_streams_generating_at_once_add_up(self):
        n = self.now
        self.observe("a1", "", n - 40, 900, 10)                  # the session: 90 tok/s
        self.observe("a2", "", n - 20, 1000, 10)                 # (two replies: 95 weighted)
        self.observe("b1", "agent-x", n - 15, 600, 10)           # its sub-agent: 60 tok/s
        self.observe("c1", "", n - 10, 800, 10, session="t")     # another session: 80 tok/s
        data = self.monitor.snapshot(n)["providers"]["claude"]
        self.assertEqual(data["display_tps"], 235.0)
        self.assertIn("3 路同时生成", data["display_basis"])
        self.assertIn("含 1 个子代理", data["display_basis"])
        self.observe("b2", "agent-x", n - 5, 300, 10, turn_end=True)    # the sub-agent finished
        self.assertEqual(self.monitor.snapshot(n)["providers"]["claude"]["display_tps"], 175.0)

    def test_one_stream_is_not_averaged_with_another(self):
        n = self.now
        self.observe("old", "", n - 200, 200, 10, session="t", turn_end=True)   # another session, done: 20 tok/s
        self.observe("new", "", n - 150, 900, 10)
        data = self.monitor.snapshot(n)["providers"]["claude"]
        self.assertEqual(data["display_tps"], 90.0)                    # only its own replies


if __name__ == "__main__":
    unittest.main()
