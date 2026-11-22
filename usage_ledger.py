"""Daily token totals read from the agents' own logs, counted the way ccusage
(github.com/ccusage/ccusage) counts them, so a day is complete whether or not
Circlow was running while the work happened.

- Claude Code: every transcript under the projects folders (sub-agents
  included). A reply is written once per content block and copied into
  resumed sessions, so it counts once per (message.id, requestId), the main
  session's copy before a sidechain one and then the largest usage. Its
  tokens are input + output + cache writes + cache reads.
- Codex: the rollouts' token_count events. The reply's own usage
  (last_token_usage) when the running total moved, else the difference of
  the totals; a repeated event with an unchanged total adds nothing, and a
  forked rollout's replayed events (same time and totals) count once. Its
  tokens are the logged total_tokens (input already holds the cache hits).
  Gemini replies through Codex are left to the Antigravity side.
- Grok CLI: each session's usage.json, per turn and model (totalTokens).

Each reply also keeps its parts (uncached input, cache writes and reads,
output) and model, priced in dollars by pricing; Grok's turns carry xAI's
own cost. Each reply counts on the local day of its own timestamp. Files are re-read
only when they change, and Claude and Codex logs only from where the last
read stopped. Only numbers stay in memory: no text from the logs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pricing
from tps_sources import provider_for, timestamp
from usage_store import local_day

LEDGER_DAYS = 15                # days kept: the board's two weeks and today
REFRESH_SECONDS = 60
SWEEP_SECONDS = 1800            # every Grok session folder is looked into this often
CLAUDE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class _File:
    __slots__ = ("version", "offset", "entries", "model", "previous")

    def __init__(self):
        self.version: tuple = ()
        self.offset = 0
        self.entries: dict[Any, tuple] = {}
        self.model = ""                     # Codex: the model of the turn being read
        self.previous: dict | None = None   # Codex: the last running total seen


def _number(value) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _claude_provider(model: str) -> str:
    provider = provider_for(model or "")
    return "claude" if provider == "other" else provider


class UsageLedger:
    def __init__(self, claude_dirs, codex_homes, grok_root: Path | None, days: int = LEDGER_DAYS):
        self.claude_dirs = [Path(path) for path in dict.fromkeys(map(str, claude_dirs))]
        self.codex_homes = [Path(path) for path in dict.fromkeys(map(str, codex_homes))]
        self.grok_root = Path(grok_root) if grok_root else None
        self.days = days
        self.files: dict[Path, _File] = {}
        self.lock = threading.Lock()
        self.totals: dict[str, dict[int, int]] = {}     # provider -> local midnight -> tokens
        self.costs: dict[str, dict[int, float]] = {}    # provider -> local midnight -> US dollars
        self.unpriced: dict[str, dict[int, int]] = {}   # tokens of models without a price
        self.refreshed_at = 0.0
        self.grok_swept = 0.0

    @classmethod
    def for_home(cls, home: Path | None = None) -> "UsageLedger":
        """The folders Circlow's monitor reads (CLAUDE_CONFIG_DIR, CODEX_HOME,
        GROK_HOME), plus the default Claude folders ccusage also reads."""
        import providers
        home = home or Path.home()
        claude = [providers.claude_home() / "projects", home / ".claude/projects", home / ".config/claude/projects"]
        codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        grok = Path(os.environ.get("GROK_HOME") or home / ".grok")
        return cls(claude, [codex], grok)

    # ----- reading -----------------------------------------------------------

    def _candidates(self, since: float) -> list[tuple[Path, str]]:
        found = []
        def walk(root: Path, pattern: str, kind: str):
            if not root.is_dir():
                return
            for path in (root.glob(pattern) if "/" in pattern else root.rglob(pattern)):
                try:
                    if path.stat().st_mtime >= since:
                        found.append((path, kind))
                except OSError:
                    continue
        for root in self.claude_dirs:
            walk(root, "*.jsonl", "claude")
        for home in self.codex_homes:
            for folder in ("sessions", "archived_sessions"):
                walk(home / folder, "rollout-*.jsonl", "codex")
        if self.grok_root is not None:
            found += [(path, "grok") for path in self._grok_files(since)]
        return found

    def _grok_files(self, since: float) -> list[Path]:
        """sessions/<folder>/<session>/usage.json of the sessions touched
        since then. Thousands of session folders hold tens of thousands of
        other files: a folder's own time (listed with it, no extra call)
        says whether to look inside, and every SWEEP_SECONDS all are looked at."""
        now = time.time()
        sweep = now - self.grok_swept >= SWEEP_SECONDS
        found = []
        try:
            folders = [entry.path for entry in os.scandir(self.grok_root / "sessions") if entry.is_dir()]
        except OSError:
            return found
        for folder in folders:
            try:
                sessions = [entry for entry in os.scandir(folder) if entry.is_dir()]
            except OSError:
                continue
            for session in sessions:
                try:
                    if not sweep and session.stat().st_mtime < since:
                        continue
                    path = Path(session.path) / "usage.json"
                    if path.stat().st_mtime >= since:
                        found.append(path)
                except OSError:
                    continue
        if sweep:
            self.grok_swept = now
        return found

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        start = local_day(now) - (self.days - 1) * 86400 - 3600
        candidates = self._candidates(start)
        current = {path for path, _ in candidates}
        for path, kind in candidates:
            state = self.files.get(path) or _File()
            try:
                info = path.stat()
            except OSError:
                continue
            version = (info.st_mtime_ns, info.st_size)
            if state.version == version:
                continue
            try:
                if kind == "grok":
                    self._read_grok(path, state)
                else:
                    if info.st_size < state.offset:          # rewritten: read it again from the start
                        state = _File()
                    self._read_lines(path, state, kind)
            except (OSError, ValueError):
                continue
            state.version = version
            self.files[path] = state
        for path in [path for path in self.files if path not in current]:
            del self.files[path]
        self._total(start)
        self.refreshed_at = now

    def _read_lines(self, path: Path, state: _File, kind: str) -> None:
        with open(path, "rb") as handle:
            handle.seek(state.offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return                                  # nothing complete yet
        state.offset += end + 1
        for raw in data[:end].splitlines():
            if kind == "claude" and b'"usage"' not in raw or kind == "codex" and b"token_count" not in raw and b"turn_context" not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                (self._claude if kind == "claude" else self._codex)(record, state, path)

    def _claude(self, record: dict, state: _File, path: Path) -> None:
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        moment = timestamp(record.get("timestamp"))
        if not isinstance(usage, dict) or moment is None:
            return
        tokens = sum(_number(usage.get(name)) for name in CLAUDE_FIELDS)
        if tokens <= 0:
            return
        mid, request = message.get("id"), record.get("requestId")
        if mid and request:
            entry_key = (mid, request)
        elif mid:
            entry_key = (mid, record.get("sessionId"), record.get("timestamp"))
        else:
            entry_key = (str(path), state.offset, len(state.entries))
        model = str(message.get("model") or "")
        writes = _number(usage.get("cache_creation_input_tokens"))
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        hour = min(writes, _number(split.get("ephemeral_1h_input_tokens")))
        entry = (not record.get("isSidechain"), tokens, moment, _claude_provider(model), model,
                 _number(usage.get("input_tokens")), _number(usage.get("output_tokens")), writes - hour, hour,
                 _number(usage.get("cache_read_input_tokens")), None)
        old = state.entries.get(entry_key)
        if old is None or entry[:2] > old[:2]:
            state.entries[entry_key] = entry

    def _codex(self, record: dict, state: _File, path: Path) -> None:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "turn_context":
            model = payload.get("model") or payload.get("model_name") or (payload.get("metadata") or {}).get("model")
            if isinstance(model, str) and model.strip():
                state.model = model.strip()
            return
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        total = total if isinstance(total, dict) else None
        last = last if isinstance(last, dict) else None
        if total is None and last is None:
            return
        if total is not None and total == state.previous:
            return                                  # the same count repeated
        if last is not None:
            usage = last
        else:
            previous = state.previous or {}
            usage = {name: _number(value) - _number(previous.get(name)) for name, value in total.items()}
        if total is not None:
            state.previous = total
        tokens = _number(usage.get("total_tokens")) or _number(usage.get("input_tokens")) + _number(usage.get("output_tokens"))
        moment = timestamp(record.get("timestamp"))
        model = state.model or "gpt-5"
        provider = provider_for(model)
        if tokens <= 0 or moment is None or provider == "gemini":
            return
        provider = "codex" if provider == "other" else provider
        running = _number((total or {}).get("total_tokens"))
        cached = min(_number(usage.get("cached_input_tokens")), _number(usage.get("input_tokens")))
        # A forked rollout replays its parent's events: same time, same totals.
        state.entries[(record.get("timestamp"), running, tokens)] = (
            True, tokens, moment, provider, model, _number(usage.get("input_tokens")) - cached,
            _number(usage.get("output_tokens")), 0, 0, cached, None)

    def _read_grok(self, path: Path, state: _File) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        session = value.get("sessionId") or path.parent.name
        entries = {}
        for turn in value.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            moment = timestamp(turn.get("endedAt"))
            if moment is None:
                continue
            models = turn.get("modelUsage") if isinstance(turn.get("modelUsage"), dict) else {}
            if not models:
                models = {turn.get("primaryModelId") or "grok": turn}
            for model, usage in models.items():
                if not isinstance(usage, dict):
                    continue
                tokens = _number(usage.get("totalTokens")) or _number(usage.get("inputTokens")) + _number(usage.get("outputTokens"))
                provider = provider_for(str(model))
                cached = min(_number(usage.get("cachedReadTokens")), _number(usage.get("inputTokens")))
                ticks = usage.get("costUsdTicks")
                usd = ticks * pricing.USD_PER_TICK if isinstance(ticks, (int, float)) and not isinstance(ticks, bool) and ticks >= 0 else None
                if tokens > 0:
                    entries[(session, turn.get("turnNumber"), model)] = (
                        True, tokens, moment, "grok" if provider == "other" else provider, str(model),
                        _number(usage.get("inputTokens")) - cached, _number(usage.get("outputTokens")),
                        _number(usage.get("cacheCreationTokens")), 0, cached, usd)
        state.entries = entries

    def _total(self, start: float) -> None:
        best: dict[Any, tuple] = {}
        for state in self.files.values():
            for entry_key, entry in state.entries.items():
                if entry[2] < start:
                    continue
                old = best.get(entry_key)
                if old is None or entry[:2] > old[:2]:
                    best[entry_key] = entry
        totals: dict[str, dict[int, int]] = {}
        costs: dict[str, dict[int, float]] = {}
        unpriced: dict[str, dict[int, int]] = {}
        for _main, tokens, moment, provider, model, uncached, output, write, write_1h, read, usd in best.values():
            day = local_day(moment)
            days = totals.setdefault(provider, {})
            days[day] = days.get(day, 0) + tokens
            if usd is None:
                usd = pricing.TABLE.cost(model, uncached, output, write, read, write_1h)
            if usd is None:
                spare = unpriced.setdefault(provider, {})
                spare[day] = spare.get(day, 0) + tokens
            else:
                spent = costs.setdefault(provider, {})
                spent[day] = spent.get(day, 0.0) + usd
        with self.lock:
            self.totals, self.costs, self.unpriced = totals, costs, unpriced

    # ----- answers -----------------------------------------------------------

    def day(self, provider: str, midnight: int) -> int:
        with self.lock:
            return self.totals.get(provider, {}).get(midnight, 0)

    def cost(self, provider: str, midnight: int) -> float:
        """The day's US dollars at list prices (pricing); unpriced models add nothing."""
        with self.lock:
            return self.costs.get(provider, {}).get(midnight, 0.0)

    def covers(self, midnight: int, now: float) -> bool:
        """Whether `midnight`'s day lies within what the logs were read for."""
        return self.refreshed_at > 0 and midnight >= local_day(now) - (self.days - 1) * 86400
