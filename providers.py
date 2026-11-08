"""Single registry of quota providers.

Adding a provider means one entry here plus a quota reader in
`quota_sources.py` (and, optionally, a local token reader in
`tps_sources.py`).  Everything else - tray icons, menus, refresh intervals,
launchers and accent colours - is derived from this table.

Which providers are shown:
* `CIRCLOW_PROVIDERS=codex,grok,gemini,claude` picks an explicit list;
* Claude is opt-in through the tray menu or an explicit environment list;
* otherwise every provider with `default=True` is shown, plus detected MiMo.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))


def _choices_path() -> Path:
    return _local_appdata() / "Circlow/providers.json"


def provider_choices() -> dict[str, bool]:
    """Saved on/off switches per source (settings page). Missing = automatic."""
    try:
        stored = json.loads(_read(_choices_path()) or "{}")
    except ValueError:
        stored = {}
    choices = {str(key): bool(value) for key, value in (stored.items() if isinstance(stored, dict) else []) if isinstance(value, bool)}
    legacy = {"enabled": True, "disabled": False}.get(_read(_local_appdata() / "Circlow/claude-enabled.txt").strip())
    if "claude" not in choices and legacy is not None:
        choices["claude"] = legacy
    return choices


def set_provider_choices(changes: dict[str, bool | None]) -> None:
    choices = provider_choices()
    for key, value in changes.items():
        if value is None:
            choices.pop(key, None)
        else:
            choices[key] = bool(value)
    path = _choices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(choices, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    if "claude" in changes and changes["claude"] is not None:
        _write_claude_legacy(bool(changes["claude"]))


def _write_claude_legacy(enabled: bool) -> None:
    path = _local_appdata() / "Circlow/claude-enabled.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("enabled" if enabled else "disabled", encoding="utf-8")


def claude_choice() -> bool | None:
    """A saved switch overrides auto-detection and the environment list."""
    return provider_choices().get("claude")


def set_claude_enabled(enabled: bool) -> None:
    set_provider_choices({"claude": enabled})


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_claude() -> Path | None:
    """Claude Code: native installer, npm shim, or an explicit override."""
    override = os.environ.get("CIRCLOW_CLAUDE_EXE")
    if override:
        return Path(override)
    candidates = [
        Path.home() / (".local/bin/claude" if sys.platform == "darwin" else ".local/bin/claude.exe"),
        Path.home() / ".local/bin/claude.exe",
        Path.home() / (".claude/local/claude" if sys.platform == "darwin" else ".claude/local/claude.exe"),
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm/claude.cmd",
    ]
    found = shutil.which("claude")
    if found:
        candidates.insert(0, Path(found))
    return next((path for path in candidates if path.is_file()), None)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def claude_routes_to_mimo() -> bool:
    """Claude Code pointed at MiMo's Anthropic-compatible endpoint has no
    Claude subscription to read; its sessions are counted as MiMo instead."""
    if "xiaomimimo" in os.environ.get("ANTHROPIC_BASE_URL", ""):
        return True
    return any("xiaomimimo" in _read(claude_home() / name) for name in ("settings.json", "settings.local.json"))


def _mimo_used() -> bool:
    data = _local_appdata() / "Circlow"
    if (data / "mimo-session.bin").is_file() or (data / "mimo.json").is_file():
        return True
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if "xiaomimimo" in _read(codex_home / "config.toml") or claude_routes_to_mimo():
        return True
    return any((root / "mimocode").is_dir() for root in (Path.home() / ".local/share", _local_appdata()))


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    letter: str                      # glyph on the idle tray icon
    label: str                       # display name (menus, cards)
    window_minutes: int | None       # primary quota window, None = reported by the source
    accent: tuple[int, int, int, int]
    idle_seconds: int                # refresh interval while unused
    active_seconds: int              # refresh interval while an agent of this source works
    settle_seconds: int              # refresh this long after a generation ends
    launch: tuple[str, Callable[[], Path | str | None]]  # (kind, target resolver)
    default: bool = True
    detect: Callable[[], bool] | None = None
    source_note: str = ""
    # Used up: check rarely, then often shortly before the reset.
    exhausted_seconds: int | None = None
    near_reset_seconds: int = 30 * 60   # start checking more often this long before the reset
    near_reset_poll: int = 3 * 60       # ... at this interval (every minute in the last 5 min)
    # Repeated failures, or an account without this quota: check rarely.
    give_up_seconds: int | None = None
    hover_seconds: int = 15          # refresh interval while the pointer rests on the icon / panel

    def available(self) -> bool:
        return self.default or bool(self.detect and self.detect())


SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "codex", "C", "Codex", None, (126, 201, 166, 255), 2 * 60, 30, 5,
        ("aumid", lambda: "OpenAI.Codex_2p2nqsd0c76g0!App"), hover_seconds=5,
        source_note="Codex App Server account/rateLimits/read（5 小时 + 每周）",
    ),
    ProviderSpec(
        # Each read opens a whole Grok CLI session (up to ~35 s): widest intervals.
        "grok", "X", "Grok", 10080, (203, 208, 216, 255), 10 * 60, 2 * 60, 20,
        ("cli", lambda: Path.home() / ".grok/bin/grok.exe"), hover_seconds=30,
        source_note="Grok CLI /usage（每周）",
        exhausted_seconds=3 * 3600,
    ),
    ProviderSpec(
        "gemini", "G", "Gemini（5 小时）", 300, (164, 176, 245, 255), 2 * 60, 45, 8,
        ("gui", lambda: _local_appdata() / "Programs/antigravity/Antigravity.exe"), hover_seconds=10,
        source_note="agy --print /usage（Argon > Claude/GPT > Gemini 额度池）",
    ),
    ProviderSpec(
        # Claude Code's status line writes its 5-hour and weekly windows to a
        # local file, so a read is cheap (the /usage CLI is only a fallback;
        # the scheduler spaces reads by their measured duration).
        "claude", "A", "Claude Code", 300, (217, 119, 87, 255), 60, 20, 5,
        ("cli", find_claude), hover_seconds=5,
        default=False, detect=lambda: claude_choice() is True,
        source_note="Claude Code 实时额度（5 小时 + 每周）、会话与 TPS",
        give_up_seconds=12 * 3600,
    ),
    ProviderSpec(
        # Token Plan: one Credits pool per billing period (a whole year for
        # annual plans).  The console read is a cheap HTTPS call; local token
        # records move the figure in between (see mimo_usage.py).
        "mimo", "M", "Xiaomi MiMo", None, (255, 112, 30, 255), 5 * 60, 60, 30,
        ("url", lambda: "https://platform.xiaomimimo.com/console/plan-manage"), hover_seconds=10,
        default=False, detect=_mimo_used,
        source_note="MiMo 控制台套餐用量（登录后只读）+ 本机 Codex / Claude Code / MiMo Code 记录折算用量",
    ),
)
BY_KEY = {spec.key: spec for spec in SPECS}

# Fixed tray positions, left to right (top to bottom on a vertical taskbar).
# Positions never depend on remaining quota, reset times or exhaustion.
TRAY_POSITIONS: tuple[str, ...] = ("claude", "codex", "gemini", "grok", "mimo")


def tray_positions(keys) -> list[str]:
    """Shown providers in their fixed tray order; unknown keys go last."""
    keys = list(keys)
    rank = {key: index for index, key in enumerate(TRAY_POSITIONS)}
    return sorted(keys, key=lambda key: (rank.get(key, len(rank)), keys.index(key)))


def get(key: str) -> ProviderSpec:
    return BY_KEY[key]


def _env_list(env: str | None = None) -> set[str] | None:
    raw = os.environ.get("CIRCLOW_PROVIDERS") if env is None else env
    return {part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()} if raw else None


def enabled_keys(env: str | None = None) -> list[str]:
    """Ordered provider keys to show in the tray.  A switch saved in the
    settings page wins over the environment list, which wins over detection."""
    listed = _env_list(env)
    wanted = listed if listed is not None else {spec.key for spec in SPECS if spec.available()}
    for key, choice in provider_choices().items():
        if choice is True:
            wanted.add(key)
        elif choice is False:
            wanted.discard(key)
    return [spec.key for spec in SPECS if spec.key in wanted]


DESCRIPTIONS = {
    "codex": "读取 Codex App Server 的 5 小时与每周额度；需要已安装并登录 Codex。",
    "grok": "读取 Grok CLI /usage 每周额度；需要已安装并登录 Grok CLI。",
    "gemini": "用 agy（Antigravity CLI）读取 Argon、Claude/GPT、Gemini 额度池；需要已安装并登录 Antigravity。",
    "claude": "读取 Claude Code 实时额度、TPS 和本地会话；使用当前已登录的订阅账号。",
    "mimo": "读取小米 MiMo Token Plan 控制台套餐；在下方登录控制台，或手动填写套餐。",
}


def provider_states(env: str | None = None) -> list[dict]:
    """Every source with whether it is shown and why, for the settings page and diagnostics."""
    listed = _env_list(env)
    choices = provider_choices()
    shown = set(enabled_keys(env))
    states = []
    for spec in SPECS:
        choice = choices.get(spec.key)
        if choice is not None:
            reason = "手动开启" if choice else "手动关闭"
        elif listed is not None:
            reason = "环境变量 CIRCLOW_PROVIDERS " + ("包含" if spec.key in listed else "未包含")
        elif spec.default:
            reason = "默认开启"
        elif spec.key == "claude":
            reason = "默认关闭（开通订阅后手动开启）"
        else:
            reason = "已检测到，自动开启" if spec.key in shown else "未检测到，默认关闭"
        states.append({"key": spec.key, "letter": spec.letter, "label": spec.label, "enabled": spec.key in shown,
                       "reason": reason, "manual": choice is not None, "description": DESCRIPTIONS.get(spec.key, "")})
    return states


def claude_desktop_aumid() -> str | None:
    """The installed Claude desktop app's launch id ("Claude_<publisher>!Claude",
    for shell:AppsFolder), or None when it is not installed. The Store package
    keeps its data under %LOCALAPPDATA%\\Packages\\Claude_<publisher>; an
    explicit id can be given with CIRCLOW_CLAUDE_APP."""
    override = os.environ.get("CIRCLOW_CLAUDE_APP")
    if override:
        return override
    if sys.platform != "win32":
        return None
    packages = _local_appdata() / "Packages"
    try:
        families = sorted(path.name for path in packages.glob("Claude_*") if path.is_dir())
    except OSError:
        return None
    return f"{families[0]}!Claude" if families else None
