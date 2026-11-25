"""Which account each agent is signed in to on this device, and switching it.

Codex, Claude Code and Grok name their account in their own sign-in files;
only identity fields are read there, never a token. agy (Gemini) names it in
the log of a /usage run. Outside this module an account is a fingerprint
(cli_usage.account_key); the masked address is for the settings window alone
and is neither stored nor synced.

Switching runs the agent's own sign-in command in a terminal window, where
the user signs in. Circlow copies and keeps no credentials.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import app_identity
import cli_usage
import providers

log = logging.getLogger("Circlow")


@dataclass(frozen=True)
class Account:
    key: str                # the fingerprint
    name: str = ""          # the masked address
    plan: str = ""


def _account(provider: str, identity, email="", plan="") -> Account | None:
    identity = str(identity or email or "")
    if not identity:
        return None
    return Account(cli_usage.account_key(f"{provider}:{identity}"), cli_usage.mask_email(str(email or "")), str(plan or ""))


# ----- reading -------------------------------------------------------------------

def _codex_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def _claude_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"


def _grok_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def _claims(token) -> dict:
    """The payload of a JWT, which says whose it is (not verified: only shown)."""
    part = str(token or "").split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _codex(data: dict) -> Account | None:
    tokens = data.get("tokens") or {}
    if not tokens:
        return Account(cli_usage.account_key("codex:api-key"), "API Key") if data.get("OPENAI_API_KEY") else None
    claims = _claims(tokens.get("id_token"))
    auth = claims.get("https://api.openai.com/auth") or {}
    return _account("codex", tokens.get("account_id") or auth.get("chatgpt_account_id"), claims.get("email"),
                    str(auth.get("chatgpt_plan_type") or "").replace("_", " ").title())


def _claude(data: dict) -> Account | None:
    owner = data.get("oauthAccount") or {}
    identity = ":".join(str(owner[name]) for name in ("accountUuid", "organizationUuid") if owner.get(name))
    return _account("claude", identity, owner.get("emailAddress"))


def _grok(data: dict) -> Account | None:
    entries = [entry for entry in data.values() if isinstance(entry, dict) and (entry.get("user_id") or entry.get("email"))]
    if not entries:
        return None
    entry = max(entries, key=lambda item: str(item.get("create_time") or ""))
    return _account("grok", entry.get("user_id"), entry.get("email"))


FILES: dict[str, tuple[Callable[[], Path], Callable[[dict], Account | None]]] = {
    "codex": (_codex_path, _codex), "claude": (_claude_path, _claude), "grok": (_grok_path, _grok)}
_CACHE: dict[str, tuple[tuple, Account | None]] = {}


def current(provider: str) -> Account | None:
    """The account in the agent's sign-in file, read again when it changes;
    None when signed out, unreadable, or the agent keeps no such file."""
    if provider not in FILES:
        return None
    locate, parse = FILES[provider]
    path = locate()
    try:
        info = path.stat()
        stamp = (str(path), info.st_mtime_ns, info.st_size)
        held = _CACHE.get(provider)
        if held is not None and held[0] == stamp:
            return held[1]
        account = parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, LookupError, AttributeError, TypeError):
        return None
    _CACHE[provider] = (stamp, account)
    return account


def key(provider: str) -> str | None:
    account = current(provider)
    return account.key if account is not None else None


def read(provider: str) -> Account | None:
    """As `current`; for Gemini, by asking agy (a few seconds)."""
    if provider != "gemini":
        return current(provider)
    parsed = cli_usage.run_agy_usage(str(executable("gemini") or "agy"))
    return Account(parsed["account"], parsed.get("account_name") or "") if parsed.get("account") else None


# ----- switching -----------------------------------------------------------------

LOGIN = {"codex": ("login",), "claude": ("auth", "login"), "grok": ("login",), "gemini": ("acp", "--login")}
LOGIN_WAIT_SECONDS = 15 * 60       # a sign-in not finished by then is no longer waited for
_CMD = "@echo off\r\nchcp 65001 >nul\r\ncall {command}\r\necho %errorlevel%> \"{marker}\"\r\npause\r\n"


def executable(provider: str) -> Path | None:
    """The agent's command-line program; None when it is not installed."""
    if provider == "codex":
        return providers.find_codex()
    if provider == "claude":
        return providers.find_claude()
    mac = sys.platform == "darwin"
    name, override, usual = {
        "grok": ("grok", "CIRCLOW_GROK_EXE", ".grok/bin/grok" if mac else ".grok/bin/grok.exe"),
        "gemini": ("agy", "CIRCLOW_GEMINI_EXE", ".local/bin/agy" if mac else "AppData/Local/agy/bin/agy.exe")}[provider]
    found = os.environ.get(override) or shutil.which(name) or str(Path.home() / usual)
    return Path(found) if Path(found).is_file() else None


def marker(provider: str) -> Path:
    """Written by the sign-in window when its command ends (its exit code)."""
    return app_identity.data_dir() / "account-login" / f"{provider}.done"


def login_script(command: list[str], done: Path) -> str:
    """macOS: the sign-in, its result noted, the window kept until a key is pressed."""
    return (f"{shlex.join(command)}; code=$?; print -r -- $code > {shlex.quote(str(done))}; print; "
            "if (( code )); then print '登录未完成'; else print '登录完成，环流会自动读取新账号'; fi; "
            "read -sk 1 '?按任意键关闭'")


def switch(provider: str, popen=subprocess.Popen) -> Path:
    """Open the agent's own sign-in in a terminal (Ghostty, else Terminal, on
    macOS; a console on Windows); returns the marker file it will write."""
    program = executable(provider)
    if program is None:
        raise FileNotFoundError("未找到命令行程序")
    done = marker(provider)
    done.parent.mkdir(parents=True, exist_ok=True)
    done.unlink(missing_ok=True)
    command = [str(program), *LOGIN[provider]]
    if sys.platform == "darwin":
        import herdr_source
        popen(herdr_source.mac_terminal(["/bin/zsh", "-lc", login_script(command, done)], str(Path.home())),
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        script = done.with_suffix(".cmd")
        script.write_text(_CMD.format(command=subprocess.list2cmdline(command), marker=done), encoding="utf-8", newline="")
        popen(["cmd.exe", "/d", "/c", str(script)], cwd=str(Path.home()), close_fds=True,
              creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    log.info("sign-in opened for %s", provider)
    return done


def wait(provider: str, stop: Callable[[float], bool], timeout: float = LOGIN_WAIT_SECONDS) -> bool | None:
    """Until the sign-in window's command ends: True when it signed in, False
    when it did not, None when it was not finished in time. `stop` waits that
    many seconds and answers whether to give up."""
    done, deadline = marker(provider), time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = done.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            text = ""
        if text:
            done.unlink(missing_ok=True)
            return text == "0"
        if stop(1.0):
            break
    return None
