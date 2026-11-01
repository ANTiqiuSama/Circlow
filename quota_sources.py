from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import providers
import cli_quota
import process_jail
from cli_usage import run_agy_usage


CACHE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray" / "state.json"


@dataclass
class QuotaSnapshot:
    provider: str
    label: str
    window_minutes: int | None
    used_percent: float | None
    remaining_percent: float | None
    resets_at: int | None
    updated_at: float | None
    stale: bool
    error: str | None
    # Optional second window (Codex reports a weekly limit next to the 5 h one).
    secondary_window_minutes: int | None = None
    secondary_used_percent: float | None = None
    secondary_remaining_percent: float | None = None
    secondary_resets_at: int | None = None
    # Separate model pools (Antigravity: Argon, Claude/GPT, Gemini). The
    # primary fields above mirror the first pool, which the ring shows.
    pools: list[dict[str, Any]] | None = None
    today_used: float | None = None          # MiMo Credits, not a daily quota
    today_date: str | None = None            # local date; do not reuse yesterday's total
    credits_balance: float | None = None     # Codex purchased credits left; None when not reported
    reset_credits: int | None = None         # Codex free full resets (5 h + weekly) available

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if isinstance(self.credits_balance, (int, float)) and math.isinf(self.credits_balance):
            result['credits_balance'] = None
            result['credits_unlimited'] = True
        return result

    @property
    def display_remaining(self) -> float | None:
        """The binding window: whichever has less quota left."""
        values = [v for v in (self.remaining_percent, self.secondary_remaining_percent) if v is not None]
        return min(values) if values else None


LABELS = {spec.key: (spec.label, spec.window_minutes) for spec in providers.SPECS}
SECONDARY_FIELDS = ("secondary_window_minutes", "secondary_used_percent", "secondary_remaining_percent", "secondary_resets_at")


def placeholder(provider: str, error: str = "正在读取…") -> QuotaSnapshot:
    label, window = LABELS[provider]
    return _unknown(provider, label, window, error)


def _unknown(provider: str, label: str, window_minutes: int | None, error: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider=provider,
        label=label,
        window_minutes=window_minutes,
        used_percent=None,
        remaining_percent=None,
        resets_at=None,
        updated_at=time.time(),
        stale=False,
        error=error,
    )


def _codex_executable() -> str | None:
    configured = os.environ.get("CODEX_QUOTA_CODEX_EXE")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("codex")
    if found:
        return found
    root = Path.home() / "AppData/Local/OpenAI/Codex/bin"
    candidates = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else None


class _JsonRpcProcess:
    def __init__(self, command: list[str]):
        # codex.CMD -> cmd.exe -> node -> codex app-server -> MCP servers: the
        # whole tree runs in one job and ends with close().
        self.process = process_jail.popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._lock = threading.Lock()
        self._messages: dict[int | str, dict[str, Any]] = {}
        self._events: dict[int | str, threading.Event] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = message.get("id")
            if key is None:
                continue
            with self._lock:
                self._messages[key] = message
                event = self._events.get(key)
            if event:
                event.set()

    def request(self, method: str, params: Any, timeout: float = 8.0) -> dict[str, Any]:
        request_id = int(time.time_ns())
        event = threading.Event()
        with self._lock:
            self._events[request_id] = event
        assert self.process.stdin is not None
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        with self._lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        if not event.wait(timeout):
            with self._lock:
                self._events.pop(request_id, None)
                self._messages.pop(request_id, None)
            raise TimeoutError(f"{method} timed out")
        with self._lock:
            message = self._messages.pop(request_id)
            self._events.pop(request_id, None)
        if "error" in message:
            raise RuntimeError(str(message["error"].get("message", message["error"])))
        return message.get("result", {})

    def close(self) -> None:
        # Always, even when the launcher already exited: node, codex and its MCP
        # servers outlive cmd.exe, and only ending the job reaches them.
        process_jail.stop(self.process)
        self._reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


def _percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return max(0.0, min(100.0, float(value)))


def _codex_credits(data: Any, has: str, balance: str) -> float | None:
    """Purchased credits left: 0 without credits, inf when unlimited, None if not reported."""
    if not isinstance(data, dict) or not isinstance(data.get(has), bool):
        return None
    if data.get("unlimited") is True:
        return float("inf")
    if not data.get(has):
        return 0.0
    try:
        if data.get(balance) is None:
            return None
        value = float(data[balance])
        return max(0.0, value) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_codex_rate_limits(bucket: dict[str, Any], reset_credits: Any = None) -> QuotaSnapshot:
    window = bucket.get("primary") or {}
    used_value = _percent(window.get("usedPercent"))
    if used_value is None:
        raise ValueError("Codex 额度响应缺少 usedPercent")
    secondary = bucket.get("secondary") or {}
    secondary_used = _percent(secondary.get("usedPercent")) if isinstance(secondary, dict) else None
    return QuotaSnapshot(
        provider="codex",
        label="Codex",
        window_minutes=window.get("windowDurationMins"),
        used_percent=used_value,
        remaining_percent=100.0 - used_value,
        resets_at=window.get("resetsAt"),
        updated_at=time.time(),
        stale=False,
        error=None,
        secondary_window_minutes=secondary.get("windowDurationMins") if secondary_used is not None else None,
        secondary_used_percent=secondary_used,
        secondary_remaining_percent=None if secondary_used is None else 100.0 - secondary_used,
        secondary_resets_at=secondary.get("resetsAt") if secondary_used is not None else None,
        credits_balance=_codex_credits(bucket.get("credits"), "hasCredits", "balance"),
        reset_credits=reset_credits if isinstance(reset_credits, int) and not isinstance(reset_credits, bool) and reset_credits >= 0 else None,
    )


def parse_codex_session_limits(limits: dict[str, Any], moment: float) -> QuotaSnapshot | None:
    """Rate limits Codex writes into its session records (token_count events,
    from the response headers of the request that just finished)."""
    def window(data):
        if not isinstance(data, dict):
            return None
        used = _percent(data.get("used_percent"))
        if used is None:
            return None
        reset = data.get("resets_at")
        if reset is None and data.get("resets_in_seconds") is not None:
            reset = moment + float(data["resets_in_seconds"])
        return used, data.get("window_minutes"), int(reset) if reset else None
    primary, secondary = window(limits.get("primary")), window(limits.get("secondary"))
    if primary is None:
        return None
    return QuotaSnapshot("codex", "Codex", primary[1], primary[0], 100.0 - primary[0], primary[2], moment, False, None,
                         secondary[1] if secondary else None, secondary[0] if secondary else None,
                         None if secondary is None else 100.0 - secondary[0], secondary[2] if secondary else None,
                         credits_balance=_codex_credits(limits.get("credits"), "has_credits", "balance"))


def get_codex_quota(raw: dict | None = None) -> QuotaSnapshot:
    executable = _codex_executable()
    if not executable:
        return _unknown("codex", "Codex", None, "未找到 Codex CLI（可设置 CODEX_QUOTA_CODEX_EXE）")
    rpc: _JsonRpcProcess | None = None
    try:
        rpc = _JsonRpcProcess([executable, "app-server", "--stdio"])
        rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "CodexQuotaTray", "title": "Codex Quota Tray", "version": "1.0.0"},
                "capabilities": {"optOutNotificationMethods": ["account/rateLimits/updated"]},
            },
        )
        result = rpc.request("account/rateLimits/read", None)
        if raw is not None:
            raw["response"] = result
        buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits", {})}
        bucket = buckets.get("codex") or result.get("rateLimits") or {}
        return parse_codex_rate_limits(bucket, (result.get("rateLimitResetCredits") or {}).get("availableCount"))
    except Exception as exc:
        return _unknown("codex", "Codex", None, f"读取失败：{type(exc).__name__}: {exc}")
    finally:
        if rpc:
            rpc.close()


def get_grok_quota(raw: dict | None = None) -> QuotaSnapshot:
    default = "grok" if sys.platform == "darwin" else "grok.exe"
    executable = os.environ.get("CODEX_QUOTA_GROK_EXE") or shutil.which("grok") or str(Path.home() / ".grok/bin" / default)
    try:
        parsed = cli_quota.read_usage(executable, "grok", raw)
        return QuotaSnapshot("grok", "Grok", parsed["window_minutes"], parsed["used_percent"], parsed["remaining_percent"], parsed["resets_at"], time.time(), False, None)
    except Exception as exc:
        return _unknown("grok", "Grok", 10080, f"读取 /usage 失败：{type(exc).__name__}: {exc}")


def get_gemini_quota() -> QuotaSnapshot:
    executable = os.environ.get("CODEX_QUOTA_GEMINI_EXE") or shutil.which("agy") or str(
        Path.home() / (".local/bin/agy" if sys.platform == "darwin" else "AppData/Local/agy/bin/agy.exe"))
    try:
        parsed = run_agy_usage(executable)
        label = f"Gemini · {parsed['pool']}"
        if parsed["pool"] == "Gemini":
            minutes = parsed["window_minutes"]
            period = "每周" if minutes == 10080 else f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟"
            label = f"Gemini（{period}）"
        return QuotaSnapshot("gemini", label, parsed["window_minutes"], parsed["used_percent"], parsed["remaining_percent"],
                             parsed["resets_at"], time.time(), False, None, pools=parsed["pools"])
    except Exception as exc:
        return _unknown("gemini", "Gemini", None, f"读取 /usage 失败：{type(exc).__name__}: {exc}")


# The status line only runs in CLI sessions; Desktop sessions never feed it.
# Past this age a native reading no longer stands for the account.
CLAUDE_NATIVE_FRESH_SECONDS = 600
# At most one hidden /usage session this often; reads in between reuse it.
CLAUDE_PROBE_GAP_SECONDS = 120
# While someone looks at Claude (hover), a status-line reading older than this
# no longer counts as current: Desktop and web use never reach the status line.
CLAUDE_HOVER_FRESH_SECONDS = 45
CLAUDE_HOVER_PROBE_GAP_SECONDS = 30
# Sources whose icon or panel is being looked at, and when each look began (set by the tray).
WATCHED: frozenset[str] = frozenset()
HOVER_STARTED: dict[str, float] = {}
_CLAUDE_PROBE: dict[str, Any] = {}
_CLAUDE_PROBE_LOCK = threading.Lock()


def _claude_native(report: dict) -> QuotaSnapshot:
    windows = report["rate_limits"]
    primary, secondary = windows.get("five_hour", {}), windows.get("seven_day", {})
    used, second = primary.get("used_percentage"), secondary.get("used_percentage")
    seen = report.get("observed_at", report["updated_at"])
    return QuotaSnapshot("claude", "Claude Code", 300, used, None if used is None else 100.0 - used,
                         primary.get("resets_at"), seen, time.time() - seen > CLAUDE_NATIVE_FRESH_SECONDS, None,
                         10080 if secondary else None, second, None if second is None else 100.0 - second,
                         secondary.get("resets_at"))


def _claude_probe() -> QuotaSnapshot:
    executable = providers.find_claude()
    if executable is None:
        return _unknown("claude", "Claude", 300, "未找到 Claude Code（claude.exe / claude.cmd），可设置 CODEX_QUOTA_CLAUDE_EXE")
    try:
        parsed = cli_quota.read_usage(str(executable))
        return QuotaSnapshot("claude", "Claude Code", parsed["window_minutes"], parsed["used_percent"], parsed["remaining_percent"],
                             parsed["resets_at"], time.time(), False, None,
                             parsed.get("secondary_window_minutes"), parsed.get("secondary_used_percent"),
                             parsed.get("secondary_remaining_percent"), parsed.get("secondary_resets_at"))
    except Exception as exc:
        return _unknown("claude", "Claude", 300, f"读取 /usage 失败：{type(exc).__name__}: {exc}")


def _claude_probe_cached(gap: float = CLAUDE_PROBE_GAP_SECONDS) -> QuotaSnapshot:
    with _CLAUDE_PROBE_LOCK:
        cached = _CLAUDE_PROBE.get("snapshot")
        if cached is None or time.time() - _CLAUDE_PROBE["at"] >= gap:
            cached = _claude_probe()
            _CLAUDE_PROBE.update(snapshot=cached, at=time.time())
        return cached


def get_claude_quota() -> QuotaSnapshot:
    """The status line while a CLI session keeps it live, else `/usage`.
    While the icon or panel is hovered, a status line older than
    CLAUDE_HOVER_FRESH_SECONDS is checked against `/usage` too, at once on
    each new hover."""
    import claude_status
    report = claude_status.latest()
    native = _claude_native(report) if report else None
    watched = "claude" in WATCHED
    if native is not None and not native.stale and not (
            watched and time.time() - (native.updated_at or 0) > CLAUDE_HOVER_FRESH_SECONDS):
        return native
    gap = CLAUDE_PROBE_GAP_SECONDS
    if watched:
        # The first read of each new hover asks /usage itself; reads during
        # the same hover reuse it for CLAUDE_HOVER_PROBE_GAP_SECONDS.
        fresh_look = _CLAUDE_PROBE.get("at", 0.0) < HOVER_STARTED.get("claude", 0.0)
        gap = 0 if fresh_look else CLAUDE_HOVER_PROBE_GAP_SECONDS
    probed = _claude_probe_cached(gap)
    if probed.error is None:
        if native is None or (probed.updated_at or 0) >= (native.updated_at or 0):
            return probed
        return native
    # A failed read keeps the last native numbers (marked stale) on screen.
    return native or probed


_MIMO_TRACKER = None
_MIMO_LOCK = threading.Lock()


def mimo_tracker():
    """Process-wide MiMo tracker (console anchor + local ledger)."""
    global _MIMO_TRACKER
    with _MIMO_LOCK:
        if _MIMO_TRACKER is None:
            import mimo_usage
            _MIMO_TRACKER = mimo_usage.MimoTracker()
        return _MIMO_TRACKER


def mimo_snapshot(view, label: str = "Xiaomi MiMo") -> QuotaSnapshot:
    """One window: the plan's yearly Credits pool, ending at the plan expiry."""
    window = None
    if view.period_start and view.period_end and view.period_end > view.period_start:
        window = int(round((view.period_end - view.period_start) / 60))
    remaining = None if view.used_percent is None else 100.0 - view.used_percent
    has_numbers = remaining is not None
    return QuotaSnapshot(
        "mimo", label, window, view.used_percent, remaining,
        int(view.period_end) if view.period_end else None,
        view.anchor_at or (time.time() if has_numbers else None),
        # Numbers from an older console read stay visible (marked stale)
        # instead of being replaced by "unknown" while logged out.
        bool(view.error) and has_numbers,
        None if has_numbers else (view.error or "暂无 MiMo 套餐数据"),
        today_used=view.today_used,
        today_date=view.day,
    )


def get_mimo_quota() -> QuotaSnapshot:
    import mimo_usage
    tracker = mimo_tracker()
    # The macOS menu-bar build has no Keychain-backed console session yet.
    # Use a manual plan and local token ledger; never create an unencrypted cookie file.
    tracker.refresh(mimo_usage.manual_plan if sys.platform == "darwin" else mimo_usage.fetch_with_renewal)
    return mimo_snapshot(tracker.view())


def _reader(provider: str):
    # Resolved at call time so tests and callers can patch the module attribute.
    return {"codex": get_codex_quota, "grok": get_grok_quota, "gemini": get_gemini_quota, "claude": get_claude_quota,
            "mimo": get_mimo_quota}[provider]


def keep_last_good(snapshot: QuotaSnapshot, old: QuotaSnapshot | None) -> QuotaSnapshot:
    """Keep stale figures on failure, except Gemini whose CLI account may have changed."""
    if snapshot.provider == "gemini" and snapshot.error:
        # CLI logins can change between reads. Without an account identifier
        # in /usage, carrying old quota across a failed read is misleading.
        return snapshot
    if snapshot.error and old and old.used_percent is not None:
        snapshot.used_percent = old.used_percent
        snapshot.remaining_percent = old.remaining_percent
        snapshot.resets_at = old.resets_at
        snapshot.window_minutes = old.window_minutes or snapshot.window_minutes
        snapshot.updated_at = old.updated_at
        for name in SECONDARY_FIELDS:
            setattr(snapshot, name, getattr(old, name))
        snapshot.stale = True
    return snapshot


def collect(providers, previous: dict[str, QuotaSnapshot] | None = None) -> dict[str, QuotaSnapshot]:
    """Read the given providers in parallel; total time is the slowest one."""
    previous = previous or {}
    providers = list(providers)
    if not providers:
        return {}
    with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="quota") as pool:
        futures = {provider: pool.submit(_reader(provider)) for provider in providers}
    results = {}
    for provider, future in futures.items():
        try:
            snapshot = future.result()
        except Exception as exc:  # Readers already catch; this is a last resort.
            snapshot = placeholder(provider, f"读取失败：{type(exc).__name__}: {exc}")
        results[provider] = keep_last_good(snapshot, previous.get(provider))
    return results


def collect_all(previous: dict[str, QuotaSnapshot] | None = None) -> dict[str, QuotaSnapshot]:
    return collect(providers.enabled_keys(), previous)


def dump_snapshot(snapshots: dict[str, QuotaSnapshot]) -> str:
    payload = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "providers": [snapshot.to_dict() for snapshot in snapshots.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_cached_snapshots() -> dict[str, QuotaSnapshot]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        result: dict[str, QuotaSnapshot] = {}
        for item in payload.get("providers", []):
            snapshot = QuotaSnapshot(
                provider=str(item["provider"]),
                label=str(item["label"]),
                window_minutes=item.get("window_minutes"),
                used_percent=item.get("used_percent"),
                remaining_percent=item.get("remaining_percent"),
                resets_at=item.get("resets_at"),
                updated_at=item.get("updated_at"),
                stale=True,
                error=None,
                **{name: item.get(name) for name in SECONDARY_FIELDS},
                pools=item.get("pools") if isinstance(item.get("pools"), list) else None,
                today_used=item.get("today_used"),
                today_date=item.get("today_date"),
                credits_balance=float("inf") if item.get("credits_unlimited") is True else item.get("credits_balance"),
                reset_credits=item.get("reset_credits"),
            )
            result[snapshot.provider] = snapshot
        return result
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {}


def save_cached_snapshots(snapshots: dict[str, QuotaSnapshot]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(dump_snapshot(snapshots), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    except OSError:
        pass
