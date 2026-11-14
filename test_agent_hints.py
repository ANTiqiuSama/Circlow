"""MiMo hover advice: bounded requests, safe context and readable fallback."""
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import agent_hints


class HintTests(unittest.TestCase):
    def test_context_uses_only_live_agent_labels_without_usage_or_quota(self):
        agents = [
            {"provider": "codex", "live": True, "title": "修复额度预测", "doing": "运行测试",
             "request": "PRIVATE REQUEST", "folder": "C:/private/secret"},
            {"provider": "claude", "live": False, "title": "旧任务"},
            {"provider": "codex", "live": True, "schedule": "heartbeat:x", "title": "定时任务"},
        ]
        data = agent_hints.context(agents, [{"name": "Codex 引擎", "processes": 2}])
        packed = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("PRIVATE REQUEST", packed)
        self.assertNotIn("secret", packed)
        self.assertNotIn("定时任务", packed)
        self.assertNotIn("quotas", data)                                   # quota never reaches the hint
        self.assertNotIn("usage", data)
        self.assertEqual(agent_hints.local_hint(data), ("", ""))          # no stock advice without MiMo
        self.assertNotIn("hovered", data)
        prompt = agent_hints.SYSTEM_PROMPT
        for wanted in ("宏观、有新意、能启发人", "连成一个新想法", "反直觉", "收尾、准备", "具体的操作或改动", "额度", "与过去的比较"):
            self.assertIn(wanted, prompt)
        self.assertIn('"idea"', prompt)
        self.assertNotIn('"now"', prompt)

    def test_local_hint_ignores_legacy_usage_stats(self):
        data = {"tasks": [], "detected_clients": [], "usage": "今天输出 1.6M，Claude 占 48%"}
        self.assertNotIn("输出", agent_hints.local_hint(data)[1])
        self.assertNotIn("48%", agent_hints.local_hint(data)[1])

    def test_without_mimo_the_hint_stays_empty(self):
        data = agent_hints.context([], [{"name": "Claude Code", "processes": 3}])
        self.assertEqual(agent_hints.local_hint(data), ("", ""))

    def test_mimo_response_is_cached_and_key_is_never_in_prompt(self):
        seen = []
        complete = threading.Event()

        def opener(request, timeout):
            seen.append((request, timeout))
            complete.set()
            return io.BytesIO(json.dumps({"choices": [{"message": {"content":
                '{"idea":"把额度预测和多端同步合起来，做成团队共享的 AI 工作台"}'}}]}).encode())

        advisor = agent_hints.HintAdvisor({"url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                                            "key": "SECRET", "model": "mimo-v2.6-flash"}, opener)
        data = {"tasks": [{"provider": "codex", "title": "额度预测", "doing": "测试"}],
                "usage": "今天输出 80K", "detected_clients": ["Codex 引擎"]}
        self.assertEqual(advisor.lookup(data), ("", ""))                   # nothing until MiMo answers
        self.assertTrue(complete.wait(2))
        for _ in range(100):
            if not advisor.pending:
                break
            time.sleep(.01)
        idea = ("", "把额度预测和多端同步合起来，做成团队共享的 AI 工作台")
        self.assertEqual(advisor.lookup(data), idea)
        self.assertEqual(len(seen), 1)
        payload = json.loads(seen[0][0].data)
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(seen[0][1], 10)
        data["tasks"][0]["doing"] = "构建"
        data["usage"] = "今天输出 81K"                                     # figures move every second
        self.assertEqual(advisor.lookup(data), idea)                       # the suggestion stays
        self.assertEqual(len(seen), 1)
        self.assertNotIn("usage", json.loads(payload["messages"][1]["content"]))
        self.assertNotIn("80K", payload["messages"][1]["content"])

    def test_mimo_usage_advice_is_not_cached_or_displayed(self):
        def opener(request, timeout):
            return io.BytesIO(json.dumps({"choices": [{"message": {"content":
                '{"now":"正在整理任务","next":"今天输出 1.6M，Claude 占 48%"}'}}]}).encode())

        advisor = agent_hints.HintAdvisor({"url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                                          "key": "SECRET", "model": "mimo-v2.6-flash"}, opener)
        data = agent_hints.context([], [])
        advisor._fetch("test", data)
        self.assertIsNone(advisor.result)
        self.assertEqual(advisor.lookup(data), agent_hints.local_hint(data))

    def test_stock_advice_is_dropped(self):
        for reply in ('{"idea":"手头的线收尾后再开新线，节奏更稳"}', '{"idea":"可准备下一项任务，或查看刚完成的结果"}',
                      '{"next":"先梳理一下当前的工作再继续"}', '{"idea":"好"}'):
            advisor = agent_hints.HintAdvisor({"url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                                              "key": "SECRET", "model": "mimo-v2.6-flash"},
                                             lambda request, timeout, reply=reply: io.BytesIO(json.dumps(
                                                 {"choices": [{"message": {"content": reply}}]}).encode()))
            advisor._fetch("test", agent_hints.context([], []))
            self.assertIsNone(advisor.result, reply)

    def test_an_old_idea_stays_while_a_new_one_is_on_its_way(self):
        advisor = agent_hints.HintAdvisor({"url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                                          "key": "SECRET", "model": "mimo-v2.6-flash"}, lambda *a, **k: None)
        advisor.result, advisor.result_at, advisor.signature = ("", "旧的想法仍然有用"), time.time() - 900, "old"
        advisor.pending = True                                             # a request is already out
        self.assertEqual(advisor.lookup(agent_hints.context([], [])), ("", "旧的想法仍然有用"))
        advisor.result_at = time.time() - agent_hints.KEEP_SECONDS - 1
        self.assertEqual(advisor.lookup(agent_hints.context([], [])), ("", ""))

    def test_non_mimo_endpoint_uses_local_hint(self):
        advisor = agent_hints.HintAdvisor({"url": "https://example.com/chat/completions", "key": "x", "model": "m"})
        data = {"tasks": [], "usage": "", "detected_clients": []}
        self.assertEqual(advisor.lookup(data), agent_hints.local_hint(data))

    def test_history_samples_codex_claude_and_grok_without_raw_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            codex, claude, grok = (root / name for name in ("codex", "claude", "grok"))
            codex.mkdir()
            with closing(sqlite3.connect(codex / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (title TEXT, first_user_message TEXT, updated_at INTEGER)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?)",
                           ("改进构建速度", "PRIVATE sk-abcdefghijklmnopqrstu", 100))
                db.commit()
            project = claude / "projects" / "one"
            project.mkdir(parents=True)
            (project / "session.jsonl").write_text(json.dumps({"type": "summary", "summary": "整理测试资料"}) + "\n")
            session = grok / "sessions" / "2026" / "one"
            session.mkdir(parents=True)
            (session / "updates.jsonl").write_text(json.dumps({"params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"text": "研究性能瓶颈"}}}}) + "\n")
            notes = SimpleNamespace(notes={})
            monitor = SimpleNamespace(notes=notes, context={}, codex_root=codex, claude_root=claude, grok_root=grok)
            topics = agent_hints.history_topics(monitor, limit=18)
            self.assertEqual({item["provider"] for item in topics}, {"codex", "claude", "grok"})
            self.assertNotIn("PRIVATE", json.dumps(topics))
            self.assertEqual(agent_hints.history_topics(None), [])


if __name__ == "__main__":
    unittest.main()
