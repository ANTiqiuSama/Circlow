"""The hover hint: who is waiting, read locally; MiMo's pick, checked and bounded."""
import io
import json
import threading
import time
import unittest

import agent_hints
import agent_tasks

URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"


def session(key, state, minutes=3, **extra):
    base = {"key": key, "provider": "claude", "state": state, "project": "Lumen", "title": "关闭左侧屏的图标要变红",
            "first": "把右侧的标识放进显示器图标里", "asks": ["关闭左侧屏的图标要变红，并有斜杠提示"],
            "reply": "已部署，点一下左屏图标看看效果。", "steps": ["编辑 xaml_rows.inc", "构建"], "errors": [],
            "older_errors": 0, "minutes": minutes, "running_minutes": None, "at": time.time()}
    base.update(extra)
    return base


def reply(content):
    return lambda request, timeout: io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


def settle(advisor):
    for _ in range(200):
        if not advisor.pending:
            return
        time.sleep(.01)


class LocalTests(unittest.TestCase):
    def test_nothing_to_say_is_an_empty_line(self):
        self.assertEqual(agent_hints.local_hint(agent_hints.context([])), ("", ""))
        working = agent_hints.context([session("a", "working", running_minutes=4)])
        self.assertEqual(agent_hints.local_line(working), "")

    def test_waiting_sessions_in_order_with_their_minutes(self):
        data = agent_hints.context([session("a", "blocked", 7), session("b", "done", 2)])
        self.assertEqual(agent_hints.local_line(data), "Claude·关闭左侧屏的图标… 等你批准 · 7 分钟")
        data = agent_hints.context([session("b", "done", 0)])
        self.assertTrue(agent_hints.local_line(data).endswith("做完了，等你看 · 刚刚"))
        old = agent_hints.context([session("b", "done", agent_hints.DONE_MINUTES + 1)])
        self.assertEqual(agent_hints.local_line(old), "")                   # finished long ago: not waiting

    def test_secrets_and_addresses_never_leave(self):
        data = agent_hints.context([session("a", "working", asks=["key: sk-abcdefghijklmnopqrstuv mail me@example.com"])])
        packed = json.dumps(agent_hints._material(data), ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop", packed)
        self.assertNotIn("me@example.com", packed)
        self.assertNotIn('"key"', packed)                                   # session keys stay local too

    def test_prompt_rules(self):
        prompt = agent_hints.SYSTEM_PROMPT
        for wanted in ("wait", "stuck", "idea", "none", "宁缺毋滥", "herdr", "不要编造", "额度"):
            self.assertIn(wanted, prompt)


class AdvisorTests(unittest.TestCase):
    def advisor(self, content, opener=None):
        return agent_hints.HintAdvisor({"url": URL, "key": "SECRET", "model": "mimo-v2.6-flash"}, opener or reply(content))

    def test_mimo_pick_is_shown_with_its_short_name_and_the_key_stays_out(self):
        seen = []

        def opener(request, timeout):
            seen.append((json.loads(request.data), timeout, dict(request.headers)))
            return reply('{"kind":"wait","session":"s1","name":"Lumen 屏幕图标","text":"红斜杠已部署，点一次左屏图标验收"}')(request, timeout)

        advisor = self.advisor("", opener)
        data = agent_hints.context([session("a", "done", 4)])
        self.assertEqual(advisor.lookup(data)[1], "Claude·关闭左侧屏的图标… 做完了，等你看 · 4 分钟")   # local until MiMo answers
        settle(advisor)
        self.assertEqual(advisor.lookup(data)[1], "Claude·Lumen 屏幕图标：红斜杠已部署，点一次左屏图标验收 · 4 分钟")
        payload, timeout, headers = seen[0]
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(payload["model"], agent_hints.HINT_MODEL)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(timeout, agent_hints.TIMEOUT)
        self.assertEqual(len(seen), 1)
        data["sessions"][0]["minutes"] = 6                                  # minutes move, the answer stays
        self.assertTrue(advisor.lookup(data)[1].endswith("· 6 分钟"))
        self.assertEqual(len(seen), 1)

    def test_a_changed_state_drops_the_old_pick_at_once(self):
        advisor = self.advisor('{"kind":"wait","session":"s1","name":"Lumen 屏幕图标","text":"红斜杠已部署，点一次左屏图标验收"}')
        data = agent_hints.context([session("a", "done", 4)])
        advisor.lookup(data)
        settle(advisor)
        self.assertIn("验收", advisor.lookup(data)[1])
        moved = agent_hints.context([session("a", "working", 0, asks=["关闭左侧屏的图标要变红", "第二页也加斜杠"])])
        advisor.last_request_at = time.time()                               # no new answer yet
        self.assertEqual(advisor.lookup(moved)[1], "")                      # never the stale line

    def test_none_keeps_a_blocked_session_but_not_finished_work(self):
        advisor = self.advisor('{"kind":"none"}')
        done = agent_hints.context([session("a", "done", 3)])
        advisor.lookup(done)
        settle(advisor)
        self.assertEqual(advisor.lookup(done)[1], "")
        advisor = self.advisor('{"kind":"none"}')
        blocked = agent_hints.context([session("a", "blocked", 3)])
        advisor.lookup(blocked)
        settle(advisor)
        self.assertIn("等你批准", advisor.lookup(blocked)[1])

    def test_answers_that_do_not_hold_up_are_dropped(self):
        data = agent_hints.context([session("a", "working", 0), session("b", "done", 2)])
        parse = agent_hints.HintAdvisor.parse
        for content in ('{"kind":"wait","session":"s1","text":"等你确认是否继续下一步操作"}',      # s1 is working
                        '{"kind":"idea","session":"s9","text":"换一种更稳的实验设计试试看"}',      # unknown session
                        '{"kind":"idea","session":"s2","text":"多 agent 分工可以再细一点更高效"}',  # workflow advice
                        '{"kind":"idea","session":"s2","text":"把成果沉淀成可复用的知识库"}',
                        '{"kind":"idea","session":"s2","text":"剩余 40% 额度注意节省"}',
                        '{"kind":"idea","session":"s2","text":"好"}',
                        '{"kind":"idea","session":"s2","text":"' + "很长" * 30 + '"}',
                        'no json at all'):
            self.assertEqual(parse(content, data)[0], "none", content)
        kept = parse('{"kind":"idea","session":"s2","name":"VANE 论文","text":"结论只提升 0.84%，要先证明不是随机种子波动"}', data)
        self.assertEqual(kept[:2], ("idea", "b"))
        stuck = parse('{"kind":"stuck","session":"s1","text":"同一个谷歌验证报错已重试四次，该换思路"}', data)
        self.assertEqual(stuck[0], "stuck")

    def test_stuck_is_marked(self):
        advisor = self.advisor('{"kind":"stuck","session":"s1","name":"防 root 检测","text":"同一个谷歌验证报错重试了四次，该换思路"}')
        data = agent_hints.context([session("a", "working", 0, provider="codex", errors=["Exit code 1"] * 3)])
        advisor.lookup(data)
        settle(advisor)
        self.assertEqual(advisor.lookup(data)[1], "留意 Codex·防 root 检测：同一个谷歌验证报错重试了四次，该换思路")

    def test_failures_back_off_and_requests_are_spaced(self):
        calls = []

        def broken(request, timeout):
            calls.append(1)
            raise TimeoutError

        advisor = self.advisor("", broken)
        data = agent_hints.context([session("a", "blocked", 1)])
        self.assertIn("等你批准", advisor.lookup(data)[1])
        settle(advisor)
        for _ in range(5):
            self.assertIn("等你批准", advisor.lookup(data)[1])
        self.assertEqual(len(calls), 1)
        self.assertGreater(advisor.failed_until, time.time())

    def test_without_sessions_nothing_is_asked(self):
        advisor = self.advisor("", lambda *a, **k: self.fail("no request without sessions"))
        self.assertEqual(advisor.lookup(agent_hints.context([])), ("", ""))

    def test_non_mimo_endpoint_uses_only_the_local_line(self):
        advisor = agent_hints.HintAdvisor({"url": "https://example.com/v1/chat/completions", "key": "", "model": "x"},
                                          lambda *a, **k: self.fail("no request"))
        self.assertIn("等你批准", advisor.lookup(agent_hints.context([session("a", "blocked", 1)]))[1])


class NoteTests(unittest.TestCase):
    """What the notes keep for the hint, per client."""

    def test_codex_turn_reply_errors_and_end(self):
        notes = agent_tasks.AgentNotes()
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {"type": "task_started"}})
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {"type": "user_message", "message": "构思 VANE 论文的故事"}})
        notes.codex("c", {"type": "response_item", "timestamp": t, "payload": {
            "type": "function_call_output", "output": "Exit code: 1\nTraceback (most recent call last): boom"}})
        notes.codex("c", {"type": "response_item", "timestamp": t, "payload": {
            "type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "Script completed\n读到：负结果、失败的尝试"}]}})
        note = notes.notes["c"]
        self.assertEqual(len(note.errors), 1)                               # a file that mentions 失败 is no failure
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {
            "type": "task_complete", "last_agent_message": "要不要把对照实验也重跑一遍？"}})
        self.assertEqual((note.end_kind, note.reply), ("done", "要不要把对照实验也重跑一遍？"))
        self.assertEqual(note.asks[-1][1], "构思 VANE 论文的故事")
        work = notes.work([], {"c": {"model": "gpt"}}, time.time() + 60, lambda model: "codex")
        self.assertEqual((work[0]["state"], work[0]["provider"]), ("asked", "codex"))

    def test_claude_reply_tool_error_and_end_turn(self):
        notes = agent_tasks.AgentNotes()
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        notes.claude("k", {"type": "user", "timestamp": t, "message": {"content": "继续任务，并且检查两个 hint 有没有实现的必要"}})
        notes.claude("k", {"type": "user", "timestamp": t, "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "Exit code 1 permission denied"}]}})
        notes.claude("k", {"type": "assistant", "timestamp": t, "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "两个都做完了，判据记在 LEDGER.md。"}]}})
        work = notes.work([], {}, time.time() + 60)
        self.assertEqual(work[0]["state"], "done")
        self.assertEqual(work[0]["errors"], ["Exit code 1 permission denied"])
        self.assertTrue(work[0]["title"].startswith("检查两个 hint"))      # not "继续任务"

    def test_herdr_blocked_and_live_states_and_skipped_sessions(self):
        notes = agent_tasks.AgentNotes()
        now = time.time()
        for key in ("a", "b", "sub", "cron"):
            notes.ask(key, "把右侧的标识放进显示器图标里", now - 30)
        notes.notes["sub"].parent = "a"
        notes.notes["cron"].schedule_id = "x"
        notes.touch("quiet", now)                                           # no request from the user at all
        work = notes.work([{"key": "a", "herdr_status": "blocked"}, {"key": "b", "live": True}], {}, now)
        self.assertEqual([(w["key"], w["state"]) for w in work], [("a", "blocked"), ("b", "working")])


if __name__ == "__main__":
    unittest.main()
