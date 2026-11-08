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
    def test_context_uses_only_live_agent_labels_and_a_usage_line_never_quota(self):
        agents = [
            {"provider": "codex", "live": True, "title": "修复额度预测", "doing": "运行测试",
             "request": "PRIVATE REQUEST", "folder": "C:/private/secret"},
            {"provider": "claude", "live": False, "title": "旧任务"},
            {"provider": "codex", "live": True, "schedule": "heartbeat:x", "title": "定时任务"},
        ]
        data = agent_hints.context(agents, [{"name": "Codex 引擎", "processes": 2}], "今天输出 1.2M，Claude 占 70%")
        packed = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("PRIVATE REQUEST", packed)
        self.assertNotIn("secret", packed)
        self.assertNotIn("定时任务", packed)
        self.assertNotIn("quotas", data)                                   # quota never reaches the hint
        self.assertEqual(agent_hints.local_hint(data)[1], "今天输出 1.2M，Claude 占 70%")   # a usage insight
        self.assertNotIn("hovered", data)
        bare = agent_hints.context(agents, [])
        self.assertNotIn("额度", agent_hints.local_hint(bare)[1])
        self.assertIn("不要提余量不足", agent_hints.SYSTEM_PROMPT)
        self.assertIn("比昨天此时", agent_hints.SYSTEM_PROMPT)                 # told not to compare with yesterday
        self.assertIn("宏观洞察或建议", agent_hints.SYSTEM_PROMPT)
        self.assertIn("不要给具体的操作或改动建议", agent_hints.SYSTEM_PROMPT)

    def test_idle_process_does_not_claim_active_generation(self):
        data = agent_hints.context([], [{"name": "Claude Code", "processes": 3}])
        self.assertIn("暂无可确认的生成任务", agent_hints.local_hint(data)[0])

    def test_mimo_response_is_cached_and_key_is_never_in_prompt(self):
        seen = []
        complete = threading.Event()

        def opener(request, timeout):
            seen.append((request, timeout))
            complete.set()
            return io.BytesIO(json.dumps({"choices": [{"message": {"content":
                '{"now":"正在修复额度预测","next":"检查回归结果并合入"}'}}]}).encode())

        advisor = agent_hints.HintAdvisor({"url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                                            "key": "SECRET", "model": "mimo-v2.6-flash"}, opener)
        data = {"tasks": [{"provider": "codex", "title": "额度预测", "doing": "测试"}],
                "usage": "今天输出 80K", "detected_clients": ["Codex 引擎"]}
        fallback = advisor.lookup(data)
        self.assertIn("额度预测", fallback[0])
        self.assertTrue(complete.wait(2))
        for _ in range(100):
            if not advisor.pending:
                break
            time.sleep(.01)
        self.assertEqual(advisor.lookup(data), ("正在修复额度预测", "检查回归结果并合入"))
        self.assertEqual(len(seen), 1)
        payload = json.loads(seen[0][0].data)
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(seen[0][1], 10)
        data["tasks"][0]["doing"] = "构建"
        data["usage"] = "今天输出 81K"                                     # figures move every second
        self.assertEqual(advisor.lookup(data)[0], "正在修复额度预测")      # the suggestion stays
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(payload["messages"][1]["content"])["usage"], "今天输出 80K")

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
