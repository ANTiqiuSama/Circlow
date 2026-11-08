"""Xiaomi MiMo Token Plan: console reading, local Credit accounting, pacing.

Data sources, best first:

1. Console (exact, covers every device): the plan page of
   platform.xiaomimimo.com is filled by `GET /api/v1/tokenPlan/usage` and
   `GET /api/v1/tokenPlan/detail`.  They are not a public API; they need the
   Xiaomi-account session of the console.  We log in once in an app-owned
   Microsoft Edge profile (`login()`), keep only the platform's cookies,
   encrypted with Windows DPAPI, and read the two endpoints read-only.
2. Local records (real time, this PC only): the TPS reader already parses
   Codex / Claude Code / MiMo Code records; requests to `mimo-*` models are
   converted into Credits with the published price table and kept in a small
   ledger.  Between two console reads, local Credits are added on top of the
   last console figure ("anchor + increment"), so the tray moves immediately.
3. Manual (no login): `mimo.json` with the plan total and period end; usage
   then comes from the local ledger only.

Nothing here sends model requests: Token Plan keys may only be used from
coding tools, and probing with them would also spend Credits.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

PLATFORM = os.environ.get("CIRCLOW_MIMO_PLATFORM", "https://platform.xiaomimimo.com")
CONSOLE_URL = PLATFORM + "/console/plan-manage"
USAGE_PATH = "/api/v1/tokenPlan/usage"
DETAIL_PATH = "/api/v1/tokenPlan/detail"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow"
COOKIE_PATH = DATA_DIR / "mimo-session.bin"
LEDGER_PATH = DATA_DIR / "mimo-ledger.json"
CONFIG_PATH = DATA_DIR / "mimo.json"
PROFILE_DIR = DATA_DIR / "mimo-edge-profile"
BEIJING = timezone(timedelta(hours=8))
LEDGER_DAYS = 40

# Credits per token: (input cache hit, input cache miss, output).  Source:
# https://platform.xiaomimimo.com/docs/zh-CN/price/tokenplan/subscription
PRICES = {
    "mimo-v2.6-pro": (2.5, 300, 600),
    "mimo-v2.6-flash": (2, 100, 200),
    "mimo-v2.5-pro": (2.5, 300, 600),
    "mimo-v2.5": (2, 100, 200),
}
NIGHT_FACTOR = 0.8  # 00:00-08:00 Beijing time


class LoginRequired(RuntimeError):
    pass


# ----- Credits -----------------------------------------------------------------

def price_for(model: str) -> tuple[tuple[float, float, float], bool]:
    """(prices, exact).  Unknown MiMo variants are charged like pro (upper bound)."""
    name = (model or "").lower().rsplit("/", 1)[-1]
    for key in sorted(PRICES, key=len, reverse=True):
        if name.startswith(key):
            return PRICES[key], True
    if "flash" in name:
        return PRICES["mimo-v2.6-flash"], False
    return PRICES["mimo-v2.6-pro"], False


def is_night(moment: float) -> bool:
    return datetime.fromtimestamp(moment, BEIJING).hour < 8


def credits(model: str, cache_hit: int, cache_miss: int, output: int, moment: float) -> tuple[float, bool]:
    (hit, miss, out), exact = price_for(model)
    value = cache_hit * hit + cache_miss * miss + output * out
    return value * (NIGHT_FACTOR if is_night(moment) else 1.0), exact


def credits_text(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f} 亿"
    if abs(value) >= 1e7:
        return f"{value / 1e4:.0f} 万"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.1f} 万"
    return f"{value:.0f}"


# ----- DPAPI -------------------------------------------------------------------

def _protect(data: bytes, decrypt: bool = False) -> bytes:
    """Windows DPAPI (current user); never persist plaintext console credentials."""
    if os.name != "nt":
        raise OSError("MiMo 控制台登录凭据仅支持 Windows DPAPI；macOS 请使用手动套餐")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source, target = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), Blob()
    entropy_bytes = b"CodexQuotaTray/mimo"      # the old name stays: sessions saved before the rename must still open
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    entropy = Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    call = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    args = (ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(target)) if decrypt else \
        (ctypes.byref(source), "Circlow", ctypes.byref(entropy), None, None, 0x1, ctypes.byref(target))
    if not call(*args):
        raise OSError("DPAPI 处理失败")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


# ----- macOS Keychain ----------------------------------------------------------
# On the Mac the session lives in the login keychain (encrypted by macOS, like
# DPAPI on Windows); the session file only says "in the keychain".

KEYCHAIN_SERVICE = "com.antiqiusama.circlow.mimo"
KEYCHAIN_MARKER = b"circlow:keychain\n"


def _keychain_account(path: Path) -> str:
    return "session" if path == COOKIE_PATH else f"session:{path}"


def _keychain_query(account: str) -> dict:
    import Security
    return {Security.kSecClass: Security.kSecClassGenericPassword, Security.kSecAttrService: KEYCHAIN_SERVICE,
            Security.kSecAttrAccount: account}


def _keychain_store(account: str, data: bytes) -> None:
    import Security
    from Foundation import NSData
    value = NSData.dataWithBytes_length_(data, len(data))
    query = _keychain_query(account)
    status = Security.SecItemUpdate(query, {Security.kSecValueData: value})
    if status == Security.errSecItemNotFound:
        status = Security.SecItemAdd({**query, Security.kSecValueData: value,
                                      Security.kSecAttrLabel: "环流 · 小米 MiMo 控制台会话"}, None)[0]
    if status != 0:
        raise OSError(f"无法写入钥匙串（{status}）")


def _keychain_load(account: str) -> bytes | None:
    import Security
    status, result = Security.SecItemCopyMatching(
        {**_keychain_query(account), Security.kSecReturnData: True, Security.kSecMatchLimit: Security.kSecMatchLimitOne}, None)
    return bytes(result) if status == 0 and result is not None else None


def _keychain_delete(account: str) -> None:
    import Security
    Security.SecItemDelete(_keychain_query(account))


def save_session(session: dict[str, Any], path: Path | None = None) -> None:
    """{"cookies": {...}, "headers": {...}, "api_base": "..."}, never in plain
    text: DPAPI-encrypted on Windows, in the login keychain on macOS."""
    path = path or COOKIE_PATH
    data = json.dumps(session).encode("utf-8")
    if sys.platform == "darwin":
        _keychain_store(_keychain_account(path), data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(KEYCHAIN_MARKER)
        return
    sealed = _protect(data)                  # raises elsewhere: no plaintext fallback
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sealed)


def load_session(path: Path | None = None) -> dict[str, Any]:
    path = path or COOKIE_PATH
    try:
        stored = path.read_bytes()
        if sys.platform == "darwin":
            stored = _keychain_load(_keychain_account(path)) if stored == KEYCHAIN_MARKER else None
            if stored is None:
                raise OSError("no keychain session")
            value = json.loads(stored.decode("utf-8"))
        else:
            value = json.loads(_protect(stored, decrypt=True).decode("utf-8"))
    except (OSError, ValueError, ImportError):
        return {"cookies": {}, "headers": {}, "api_base": ""}
    if not isinstance(value, dict):
        value = {}
    if "cookies" not in value and "headers" not in value:   # first format: the cookie jar itself
        value = {"cookies": value}
    clean = lambda d: {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    return {"cookies": clean(value.get("cookies")), "headers": clean(value.get("headers")),
            "api_base": str(value.get("api_base") or "")}


def save_cookies(cookies: dict[str, str], path: Path | None = None) -> None:
    session = load_session(path)
    session["cookies"] = cookies
    save_session(session, path)


def load_cookies(path: Path | None = None) -> dict[str, str]:
    return load_session(path)["cookies"]


def logout() -> None:
    import shutil
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)   # the Xiaomi account session lives there
    if sys.platform == "darwin":
        try:
            _keychain_delete(_keychain_account(COOKIE_PATH))
        except Exception:
            pass
    for path in (COOKIE_PATH,):
        try:
            path.unlink()
        except OSError:
            pass


# ----- console -----------------------------------------------------------------

@dataclass
class PlanStatus:
    total: float | None                  # Credits in the current period
    used: float | None
    plan_name: str = ""
    plan_code: str = ""
    period_start: float | None = None
    period_end: float | None = None
    settlement: str = ""
    expired: bool = False
    items: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: float = 0.0
    source: str = "console"              # console | manual


def _unwrap(payload: Any) -> Any:
    """Console responses are {code, message, data}; code 0/200 means success."""
    if isinstance(payload, dict) and "code" in payload:
        code = payload.get("code")
        if code not in (0, 200, "0", "200"):
            message = str(payload.get("message") or payload.get("msg") or code)
            if code in (401, 403, "401", "403") or any(word in message.lower() for word in ("login", "登录", "auth")):
                raise LoginRequired(message)
            raise RuntimeError(f"MiMo 控制台返回错误：{message}")
        return payload.get("data")
    return payload


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result >= 0 else None


def parse_time(value: Any) -> float | None:
    """Epoch (s or ms), ISO, or 'YYYY-MM-DD HH:MM[:SS]' (Beijing when naive)."""
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e11 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            moment = datetime.fromisoformat(candidate)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=BEIJING)
            return moment.timestamp()
        except ValueError:
            continue
    return None


YEAR_SPAN_MIN = 300 * 86400


def _year_before(end: float) -> float:
    moment = datetime.fromtimestamp(end, BEIJING)
    try:
        return moment.replace(year=moment.year - 1).timestamp()
    except ValueError:  # 29 Feb
        return (moment - timedelta(days=365)).timestamp()


def _period_start(end: float | None, settlement: str = "", subscribe: str = "") -> float | None:
    """Token Plan is tracked as one yearly pool (the console's monthly billing
    cycle, if any, is not shown): the period is the year before the end."""
    return None if end is None else _year_before(end)


def _yearly_period(start: float | None, end: float | None) -> tuple[float | None, float | None]:
    if end is None:
        return start, end
    if start is None or end - start < YEAR_SPAN_MIN:
        start = _year_before(end)
    return start, end


def parse_plan(usage_payload: Any, detail_payload: Any, now: float | None = None) -> PlanStatus:
    usage = _unwrap(usage_payload) or {}
    detail = _unwrap(detail_payload) or {}
    container = usage.get("usage") if isinstance(usage, dict) and isinstance(usage.get("usage"), dict) else usage
    items = container.get("items") if isinstance(container, dict) else container if isinstance(container, list) else None
    if not isinstance(items, list):
        items = []
    total = used = 0.0
    counted = []
    for item in items:
        if not isinstance(item, dict):
            continue
        limit, spent = _number(item.get("limit")), _number(item.get("used"))
        if limit is None and spent is not None and _number(item.get("percent")):
            percent = _number(item.get("percent"))
            percent = percent / 100 if percent > 1 else percent
            limit = spent / percent if percent else None
        if not limit:
            continue
        counted.append({"name": str(item.get("name") or ""), "used": spent or 0.0, "limit": limit})
        total += limit
        used += spent or 0.0
    if not counted and isinstance(container, dict):
        # Team-seat style payload: creditsUsed / creditsTotal / usedPercent.
        limit, spent = _number(container.get("creditsTotal")), _number(container.get("creditsUsed"))
        if limit:
            counted.append({"name": "credits", "used": spent or 0.0, "limit": limit})
            total, used = limit, spent or 0.0
    if not isinstance(detail, dict):
        detail = {}
    if not counted and not detail:
        raise ValueError("MiMo 控制台未返回套餐用量（可能尚未订阅 Token Plan）")
    settlement = str(detail.get("settlementType") or "")
    subscribe = str(detail.get("subscribeType") or "")
    # The plan's own expiry is the end of the yearly pool; a monthly
    # "currentPeriodEnd" never shortens it.
    ends = [parse_time(detail.get(k)) for k in ("currentPeriodEnd", "expiredAt", "expireTime", "expireAt", "endTime",
                                                 "planEndTime", "subscriptionEndTime", "subscribeEndTime")]
    ends = [value for value in ends if value]
    end = max(ends) if ends else None
    start, end = _yearly_period(parse_time(detail.get("currentPeriodStart") or detail.get("startTime")), end)
    return PlanStatus(
        total=total or None, used=used if counted else None,
        plan_name=str(detail.get("planName") or ""), plan_code=str(detail.get("planCode") or ""),
        period_start=start, period_end=end, settlement=settlement or subscribe,
        expired=bool(detail.get("expired")), items=counted, fetched_at=now or time.time(),
    )


def _request(path: str, session: dict[str, Any], timeout: float = 15.0) -> Any:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "zh-CN",
        "x-timeZone": "Asia/Shanghai",
        "Referer": CONSOLE_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Circlow/read-only",
    }
    headers.update(session.get("headers") or {})
    cookies = session.get("cookies") or {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    request = urllib.request.Request((session.get("api_base") or PLATFORM) + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LoginRequired(f"HTTP {error.code}") from None
        raise RuntimeError(f"MiMo 控制台 HTTP {error.code}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise LoginRequired("控制台返回了登录页面") from None


def fetch_plan(cookies: dict[str, str] | None = None, raw: dict | None = None) -> PlanStatus:
    session = load_session() if cookies is None else {"cookies": cookies}
    if not session.get("cookies") and not session.get("headers"):
        raise LoginRequired("尚未登录")
    usage = _request(USAGE_PATH, session)
    detail = _request(DETAIL_PATH, session)
    if raw is not None:
        raw.update({"via": "direct", "usage": usage, "detail": detail})
    return parse_plan(usage, detail)


def manual_plan(path: Path | None = None) -> PlanStatus | None:
    """mimo.json: {"total_credits": 49.2e9, "period_end": "2027-06-01",
    "period_start": optional, "used_credits": optional baseline,
    "used_at": optional time of that baseline}."""
    path = path or CONFIG_PATH
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    total = _number(config.get("total_credits"))
    if not total:
        return None
    end = parse_time(config.get("period_end"))
    start, end = _yearly_period(parse_time(config.get("period_start")), end)
    status = PlanStatus(total, _number(config.get("used_credits")) or 0.0, str(config.get("plan_name") or "手动配置"),
                        period_start=start, period_end=end, settlement=str(config.get("settlement") or ""), source="manual")
    status.fetched_at = parse_time(config.get("used_at")) or start or 0.0
    return status


def parse_credits(text: Any) -> float | None:
    """'49.2B', '492亿', '1,000,000', '3.5e9' -> Credits."""
    value = str(text or "").strip().replace(",", "").replace("，", "").replace(" ", "")
    if not value:
        return None
    units = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9, "g": 1e9, "G": 1e9, "万": 1e4, "亿": 1e8}
    factor = 1.0
    while value and value[-1] in units:
        factor *= units[value[-1]]
        value = value[:-1]
    try:
        number = float(value) * factor
    except ValueError:
        return None
    return number if number >= 0 else None


def save_manual_plan(total: Any, period_end: Any, used: Any = "", name: str = "", path: Path | None = None) -> dict:
    """Write mimo.json from the settings page; raises ValueError with a readable message."""
    amount = parse_credits(total)
    if not amount:
        raise ValueError("请填写套餐总量，例如 49.2B 或 492亿")
    match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", str(period_end or "").strip())
    end = parse_time(f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}") if match else None
    if end is None:
        raise ValueError("请填写到期日期，格式 YYYY-MM-DD")
    config: dict[str, Any] = {"total_credits": amount, "period_end": datetime.fromtimestamp(end, BEIJING).strftime("%Y-%m-%d")}
    spent = parse_credits(used)
    if spent:
        config["used_credits"] = spent
        config["used_at"] = datetime.now(BEIJING).isoformat(timespec="seconds")
    if name.strip():
        config["plan_name"] = name.strip()
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return config


def remove_manual_plan(path: Path | None = None) -> None:
    (path or CONFIG_PATH).unlink(missing_ok=True)


# ----- local ledger ------------------------------------------------------------

class LocalLedger:
    """Credits spent from this PC, from numeric token counts only."""

    def __init__(self, path: Path | None = None):
        self.path = path or LEDGER_PATH
        self.lock = threading.Lock()
        self.entries: list[list] = []          # [ended_at, credits, source, model, exact]
        self.seen: set[str] = set()
        self.dirty_at = 0.0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [list(e) for e in data.get("entries", []) if isinstance(e, list) and len(e) >= 4]
            self.seen = set(data.get("seen", []))
        except (OSError, ValueError, AttributeError):
            pass

    def ingest(self, observations: Iterable[Any]) -> int:
        added = 0
        with self.lock:
            for item in observations:
                if getattr(item, "provider", "") != "mimo" or item.identity in self.seen:
                    continue
                cached = int(getattr(item, "cached_tokens", None) or 0)
                miss = int(getattr(item, "input_tokens", None) or 0)
                value, exact = credits(item.model, cached, miss, int(item.output_tokens), item.ended_at)
                self.seen.add(item.identity)
                self.entries.append([item.ended_at, round(value, 1), item.source, item.model, exact])
                added += 1
            if added:
                cutoff = time.time() - LEDGER_DAYS * 86400
                self.entries = sorted((e for e in self.entries if e[0] >= cutoff), key=lambda e: e[0])
                if len(self.seen) > 50_000:
                    self.seen = set(list(self.seen)[-20_000:])
                self.dirty_at = self.dirty_at or time.time()
        return added

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not self.dirty_at or (not force and time.time() - self.dirty_at < 30):
                return
            payload = {"entries": self.entries, "seen": sorted(self.seen)}
            self.dirty_at = 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass

    def since(self, start: float, end: float | None = None) -> float:
        with self.lock:
            return sum(e[1] for e in self.entries if e[0] > start and (end is None or e[0] <= end))

    def by_source(self, start: float) -> dict[str, float]:
        result: dict[str, float] = {}
        with self.lock:
            for e in self.entries:
                if e[0] > start:
                    result[e[2]] = result.get(e[2], 0.0) + e[1]
        return dict(sorted(result.items(), key=lambda kv: -kv[1]))

    def daily(self, days: int, now: float) -> list[float]:
        start = _local_midnight(now) - (days - 1) * 86400
        totals = [0.0] * days
        with self.lock:
            for e in self.entries:
                if e[0] >= start:
                    index = min(days - 1, int((e[0] - start) // 86400))
                    totals[index] += e[1]
        return totals

    def estimated(self, start: float) -> bool:
        with self.lock:
            return any(not (e[4] if len(e) > 4 else True) for e in self.entries if e[0] > start)


def _local_midnight(now: float) -> float:
    moment = datetime.fromtimestamp(now)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ----- tracker: anchor + increment, pacing -------------------------------------

@dataclass
class MimoView:
    total: float | None
    used: float | None                     # console anchor + local increment
    remaining: float | None
    used_percent: float | None
    period_start: float | None
    period_end: float | None
    time_percent: float | None             # share of the period already elapsed
    today_used: float
    today_budget: float | None
    today_budget_left_percent: float | None
    day_marker: float                      # where today's ring "should" be (% left)
    pace_daily: float | None               # recent Credits/day
    exhausts_at: float | None
    local_today: float
    by_source: dict[str, float]
    plan_name: str
    source: str
    anchor_at: float | None
    error: str | None
    estimated: bool
    day: str = ""                          # local calendar day of today_used


class MimoTracker:
    def __init__(self, ledger: LocalLedger | None = None, history_path: Path | None = None):
        self.ledger = ledger or LocalLedger()
        self.plan: PlanStatus | None = None
        self.error: str | None = None
        self.lock = threading.Lock()
        self.day_open: dict[str, float] = {}   # local date -> first console "used" that day
        self.history_path = history_path or (DATA_DIR / "mimo-days.json")
        try:
            self.day_open = {str(k): float(v) for k, v in json.loads(self.history_path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError):
            pass

    def refresh(self, fetcher=fetch_plan) -> PlanStatus | None:
        """Read the console (or manual config). Never raises."""
        try:
            plan = fetcher()
            error = None
        except LoginRequired as exc:
            plan = manual_plan()
            error = None if plan else f"未登录 MiMo 控制台（{exc}）：打开设置 → 小米 MiMo，点“登录小米 MiMo”，或填写手动套餐"
        except Exception as exc:  # network, format change
            plan, error = None, f"读取 MiMo 控制台失败：{type(exc).__name__}: {exc}"
        with self.lock:
            if plan is not None:
                self.plan = plan
                if plan.source == "console" and plan.used is not None:
                    key = datetime.fromtimestamp(plan.fetched_at).strftime("%Y-%m-%d")
                    if key not in self.day_open:
                        self.day_open[key] = plan.used
                        self.day_open = dict(sorted(self.day_open.items())[-LEDGER_DAYS:])
                        try:
                            self.history_path.parent.mkdir(parents=True, exist_ok=True)
                            self.history_path.write_text(json.dumps(self.day_open), encoding="utf-8")
                        except OSError:
                            pass
            elif self.plan is None:
                self.plan = manual_plan()
            self.error = error if (plan is None or plan.source != "console") else None
            return self.plan

    def view(self, now: float | None = None) -> MimoView:
        now = now or time.time()
        with self.lock:
            plan, error = self.plan, self.error
            day_open = dict(self.day_open)
        midnight = _local_midnight(now)
        local_today = self.ledger.since(midnight)
        total = plan.total if plan else None
        used = None
        anchor = plan.fetched_at if plan else None
        if plan and plan.used is not None:
            # Console figure plus what this PC spent since it was read.
            used = plan.used + self.ledger.since(anchor or now)
        elif plan and plan.source == "manual" and plan.period_start:
            used = self.ledger.since(plan.period_start)
        start, end = (plan.period_start, plan.period_end) if plan else (None, None)
        remaining = None if total is None or used is None else max(0.0, total - used)
        used_percent = None if total is None or used is None else min(100.0, 100.0 * used / total)
        time_percent = None
        if start and end and end > start:
            time_percent = max(0.0, min(100.0, 100.0 * (now - start) / (end - start)))
        # Today's consumption: console delta since today's first read when we
        # have one (covers other devices), never less than what we saw locally.
        today_key = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        console_today = (used - day_open[today_key]) if used is not None and today_key in day_open else 0.0
        today_used = max(local_today, console_today, 0.0)
        today_budget = left_percent = None
        if remaining is not None and end:
            days_left = max(1.0, (end - midnight) / 86400)
            today_budget = (remaining + today_used) / days_left
            if today_budget > 0:
                left_percent = max(0.0, min(100.0, 100.0 * (1 - today_used / today_budget)))
        day_marker = max(0.0, min(100.0, 100.0 * (1 - (now - midnight) / 86400)))
        week = self.ledger.daily(7, now)
        observed_days = [value for value in week[:-1] if value > 0]
        pace = None
        if used is not None and start and now - start > 3 * 86400:
            pace = used / ((now - start) / 86400)          # whole-period average (all devices)
        if observed_days:
            recent = sum(observed_days) / len(observed_days)
            pace = max(pace or 0.0, recent)
        exhausts = None
        if remaining is not None and pace and pace > 0:
            exhausts = now + remaining / pace * 86400
        return MimoView(total, used, remaining, used_percent, start, end, time_percent, today_used, today_budget,
                        left_percent, day_marker, pace, exhausts, local_today, self.ledger.by_source(midnight),
                        plan.plan_name if plan else "", plan.source if plan else "", anchor, error,
                        self.ledger.estimated(midnight), today_key)


# ----- Edge login (Chrome DevTools Protocol over a tiny WebSocket client) -------

MAC_BROWSERS = ("Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "Chromium.app/Contents/MacOS/Chromium", "Brave Browser.app/Contents/MacOS/Brave Browser")


def edge_path() -> str | None:
    """A Chromium browser for the console login: Edge or Chrome on Windows;
    Chrome, Edge, Chromium or Brave on macOS (in /Applications or ~/Applications)."""
    override = os.environ.get("CIRCLOW_MIMO_BROWSER")
    candidates = [override] if override else []
    if sys.platform == "darwin":
        for folder in (Path("/Applications"), Path.home() / "Applications"):
            candidates += [str(folder / name) for name in MAC_BROWSERS]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA") if os.name == "nt" else None):
        if base:
            candidates += [os.path.join(base, "Microsoft/Edge/Application/msedge.exe"),
                           os.path.join(base, "Google/Chrome/Application/chrome.exe")]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


class WebSocket:
    """Just enough RFC 6455 for one local DevTools connection."""

    def __init__(self, url: str, timeout: float = 10.0):
        assert url.startswith("ws://")
        host_port, _, path = url[5:].partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools 握手失败")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("DevTools 拒绝连接")
        self.buffer = response.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    @staticmethod
    def frame(payload: bytes, opcode: int = 1) -> bytes:
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def _read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools 连接已关闭")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def receive(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 8:
                raise ConnectionError("DevTools 连接已关闭")
            if opcode in (0, 1, 2):
                message += payload
                if first & 0x80:
                    return message.decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self.next_id += 1
        ident = self.next_id
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.sock.sendall(self.frame(json.dumps(message).encode()))
        while True:
            reply = json.loads(self.receive())
            if reply.get("id") == ident:
                if "error" in reply:
                    raise RuntimeError(str(reply["error"]))
                return reply.get("result") or {}
            if "method" in reply:
                events = getattr(self, "events", None)
                if events is None:
                    events = self.events = []
                events.append(reply)
                del events[:-500]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def platform_cookies(cookies: list[dict]) -> dict[str, str]:
    """Only cookies the platform itself would receive; never account.xiaomi.com."""
    host = _host(PLATFORM)
    result = {}
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            result[str(cookie.get("name"))] = str(cookie.get("value"))
    return result


LOGIN_LOG = DATA_DIR / "mimo-login.log"
_STANDARD_HEADERS = {"accept", "accept-language", "accept-encoding", "user-agent", "referer", "origin", "cookie",
                     "content-type", "content-length", "connection", "host", "pragma", "cache-control", "priority",
                     "x-timezone", "dnt", "upgrade-insecure-requests"}

# Runs inside the console page: the browser adds its own cookies, so this works
# whatever the console uses for its session.  Returns JSON text.
PAGE_FETCH = r"""(async (paths, extra) => {
  const out = {href: location.origin + location.pathname};
  for (const [name, url] of paths) {
    try {
      const response = await fetch(url, {credentials: "include", headers: Object.assign(
        {"Accept": "application/json", "x-timeZone": Intl.DateTimeFormat().resolvedOptions().timeZone}, extra)});
      out[name + "Status"] = response.status;
      const text = await response.text();
      try { out[name] = JSON.parse(text); } catch (e) { out[name] = null; out[name + "Text"] = text.slice(0, 120); }
    } catch (e) { out[name + "Error"] = String(e); }
  }
  try { out.storageKeys = Object.keys(localStorage).slice(0, 40); } catch (e) {}
  return JSON.stringify(out);
})"""


def _log(message: str) -> None:
    """Login diagnostics: steps, status codes, cookie/header NAMES — never values."""
    try:
        LOGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOGIN_LOG.exists() and LOGIN_LOG.stat().st_size > 200_000:
            LOGIN_LOG.unlink()
        with LOGIN_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


def captured_api(events: list[dict]) -> tuple[str, dict[str, str]]:
    """From the console's own requests: the API origin and the non-standard
    request headers it sends (e.g. an Authorization token from localStorage)."""
    base, headers = "", {}
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        request = (event.get("params") or {}).get("request") or {}
        url = str(request.get("url") or "")
        if "/api/" not in url or ("xiaomimimo" not in _host(url) and _host(url) != _host(PLATFORM)):
            continue
        origin = url.split("/api/", 1)[0]
        if "tokenPlan" in url or not base:
            base = origin
        for name, value in (request.get("headers") or {}).items():
            lower = name.lower()
            if lower in _STANDARD_HEADERS or lower.startswith("sec-") or lower.startswith(":"):
                continue
            headers[name] = str(value)
    return base, headers


def _page_session(socket_: "WebSocket", attached: dict) -> str | None:
    """Attach to the console tab (once per target)."""
    host = _host(PLATFORM)
    pages = [t for t in socket_.call("Target.getTargets").get("targetInfos", []) if t.get("type") == "page"]
    page = next((t for t in pages if _host(str(t.get("url"))) == host), pages[0] if pages else None)
    if not page:
        return None
    target = page["targetId"]
    if target not in attached:
        session = socket_.call("Target.attachToTarget", {"targetId": target, "flatten": True}).get("sessionId")
        socket_.call("Network.enable", {}, session)
        attached[target] = session
        if _host(str(page.get("url"))) == host:
            # The console may have loaded before we listened: reload once so its
            # own API requests (and any token header they carry) are observed.
            try:
                socket_.call("Page.reload", {"ignoreCache": False}, session)
                time.sleep(1.5)
            except RuntimeError:
                pass
        _log(f"attached to page {_host(str(page.get('url')))}")
    attached["_url"] = str(page.get("url") or "")
    return attached[target]


def page_fetch(socket_: "WebSocket", session: str, base: str, headers: dict[str, str]) -> dict:
    paths = [["usage", base + USAGE_PATH], ["detail", base + DETAIL_PATH]]
    expression = f"{PAGE_FETCH}({json.dumps(paths)}, {json.dumps(headers)})"
    result = socket_.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                               "returnByValue": True, "timeout": 15000}, session)
    value = (result.get("result") or {}).get("value")
    return json.loads(value) if isinstance(value, str) else {}


def _summary(data: dict) -> str:
    keys = ("href", "usageStatus", "detailStatus", "usageError", "detailError", "usageText", "detailText")
    parts = [f"{k}={data[k]}" for k in keys if k in data]
    for name in ("usage", "detail"):
        payload = data.get(name)
        if isinstance(payload, dict):
            parts.append(f"{name}.code={payload.get('code')} {name}.keys={sorted((payload.get('data') or payload) if isinstance(payload.get('data') or payload, dict) else [])[:12]}")
    if data.get("storageKeys"):
        parts.append(f"storageKeys={data['storageKeys']}")
    return " ".join(parts)


def _wait_browser_port(port_file: Path, deadline: float) -> list[str]:
    # Edge may hand off to a different process (including de-elevation).
    # The launcher exit code does not describe the browser's lifetime.
    startup_deadline = min(deadline, time.monotonic() + 20.0)
    while time.monotonic() < startup_deadline:
        try:
            lines = port_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith('/devtools/'):
                return lines
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("浏览器未开启调试端口（请检查 MiMo 专用浏览器窗口及配置是否被占用）")


def login(interactive: bool = True, timeout: float = 600.0, raw: dict | None = None) -> PlanStatus:
    """Open the console in an app-owned Edge profile and read the plan from
    inside the page once logged in.  Saves what a direct read needs (platform
    cookies plus any token header the console sends).  Non-interactive mode
    runs the same profile headless: this is also the fallback reader when a
    direct read is rejected."""
    browser = edge_path()
    if not browser:
        raise RuntimeError("未找到 Microsoft Edge / Chrome，无法打开登录窗口")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    port_file = PROFILE_DIR / "DevToolsActivePort"
    try:
        port_file.unlink()
    except OSError:
        pass
    args = [browser, f"--user-data-dir={PROFILE_DIR}", "--remote-debugging-port=0", "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check", "--disable-sync"]
    args += [f"--app={CONSOLE_URL}", "--window-size=1100,820"] if interactive else ["--headless=new", CONSOLE_URL]
    _log(f"start {'interactive' if interactive else 'headless'} {os.path.basename(browser)}")
    # Edge/Chrome start GPU, network and renderer processes; run the whole tree
    # in a job so a crashed or timed-out read cannot leave any of them behind
    # (the headless renewal runs unattended).
    import process_jail
    process = process_jail.popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if not interactive else 0)
    socket_ = None
    deadline = time.monotonic() + (timeout if interactive else 45.0)
    last = ""
    try:
        lines = _wait_browser_port(port_file, deadline)
        socket_ = WebSocket(f"ws://127.0.0.1:{int(lines[0])}{lines[1].strip()}")
        socket_.events = []
        attached: dict = {}
        while time.monotonic() < deadline:
            try:
                session = _page_session(socket_, attached)
            except (RuntimeError, KeyError) as exc:
                session = None
                _log(f"attach failed: {exc}")
            on_console = session and _host(attached.get("_url", "")) == _host(PLATFORM)
            if on_console:
                base, headers = captured_api(socket_.events)
                try:
                    data = page_fetch(socket_, session, base, headers)
                except (RuntimeError, ValueError) as exc:
                    data = {"usageError": f"evaluate: {exc}"}
                summary = _summary(data)
                if summary != last:
                    _log(f"page read: {summary}; captured base={base or '-'} headers={sorted(headers)}")
                    last = summary
                try:
                    plan = parse_plan(data.get("usage"), data.get("detail"))
                except LoginRequired:
                    plan = None
                except (ValueError, RuntimeError, AttributeError) as exc:
                    if data.get("usageStatus") == 200 and isinstance(data.get("usage"), dict):
                        raise RuntimeError(f"已登录，但读取套餐失败：{exc}（详情见 mimo-login.log）") from None
                    plan = None
                if plan is not None:
                    jar = platform_cookies(socket_.call("Storage.getCookies").get("cookies", []))
                    api_host = _host(base) if base else ""
                    if api_host and api_host != _host(PLATFORM):
                        jar.update({str(c.get("name")): str(c.get("value")) for c in
                                    socket_.call("Storage.getCookies").get("cookies", [])
                                    if api_host == str(c.get("domain") or "").lstrip(".") or
                                    api_host.endswith("." + str(c.get("domain") or "").lstrip("."))})
                    save_session({"cookies": jar, "headers": headers, "api_base": base})
                    _log(f"success: cookies={sorted(jar)} headers={sorted(headers)} base={base or PLATFORM}")
                    if raw is not None:
                        raw.update({"via": "browser", "usage": data.get("usage"), "detail": data.get("detail"),
                                    "status": {k: data.get(k) for k in ("usageStatus", "detailStatus")}})
                    plan.fetched_at = time.time()
                    globals()["_headless_failed_at"] = 0.0
                    return plan
            time.sleep(2.0)
        raise LoginRequired("登录超时（详情见数据文件夹中的 mimo-login.log）" if interactive else
                            "MiMo 控制台会话已过期")
    finally:
        if socket_:
            try:
                socket_.call("Browser.close")
            except Exception:
                pass
            socket_.close()
        try:
            process.wait(timeout=5)
        except Exception:
            pass
        process_jail.stop(process)        # ends anything of the browser still running


_browser_lock = threading.Lock()
_headless_failed_at = 0.0
HEADLESS_RETRY = 6 * 3600   # a logged-out profile is not retried on every refresh


def fetch_with_renewal(raw: dict | None = None) -> PlanStatus:
    """Direct read with the saved session; when rejected (expired, or the
    console needs more than cookies) read once through the headless profile."""
    try:
        return fetch_plan(raw=raw)
    except LoginRequired as exc:
        if not PROFILE_DIR.is_dir() or not edge_path():
            raise
        _log(f"direct read rejected ({exc}); trying headless profile")
        global _headless_failed_at
        if time.time() - _headless_failed_at < HEADLESS_RETRY or not _browser_lock.acquire(blocking=False):
            raise
        try:
            return login(interactive=False, raw=raw)
        except LoginRequired:
            _headless_failed_at = time.time()
            raise
        finally:
            _browser_lock.release()


def redact(value: Any) -> Any:
    """For --check-mimo --raw: keep structure and numbers, drop identities."""
    secret = ("user", "uid", "mail", "phone", "mobile", "key", "token", "name", "nick", "avatar", "account", "id")
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in secret) and not isinstance(v, (int, float, bool, dict, list))
                    and k not in ("planName", "planCode", "name") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
