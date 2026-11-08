"""How a click on a source's icon opens that source, in order of preference.

Shared by the Windows tray and the macOS menu bar: `ways` lists what can be
tried on this machine, and each front end only knows how to carry out a way
(ShellExecute, `open -a`, a Herdr workspace, a terminal). A way with
`fallback` set may fail quietly and hand over to the next one.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import herdr_source
import providers

# Sources whose click starts the agent in a new Herdr workspace when Herdr is installed.
HERDR_AGENTS = frozenset({"claude", "grok"})
MAC_APPS = {"codex": "Codex", "gemini": "Antigravity", "claude": "Claude"}


@dataclass(frozen=True)
class Way:
    kind: str           # "aumid" | "mac-app" | "herdr" | "url" | "cli" | "gui"
    target: Any         # the launch id, app name, provider, URL, or a callable giving the executable
    fallback: bool = False   # when it fails, try the next way instead of reporting an error


def ways(provider: str, platform: str = sys.platform) -> list[Way]:
    """The ways to open `provider` here, best first. Claude's desktop app
    comes before Claude Code; Claude Code and Grok go to a Herdr workspace
    when Herdr is installed, else their CLI; the rest open their own app or
    page as the provider registry says."""
    spec = providers.get(provider)
    kind, resolve = spec.launch
    out: list[Way] = []
    if provider == "claude":
        if platform == "win32":
            aumid = providers.claude_desktop_aumid()
            if aumid:
                out.append(Way("aumid", aumid, fallback=True))
        elif platform == "darwin" and Path("/Applications/Claude.app").is_dir():
            out.append(Way("mac-app", MAC_APPS["claude"], fallback=True))
    if provider in HERDR_AGENTS and herdr_source.find_exe():
        out.append(Way("herdr", provider))
    if platform == "darwin":
        if provider in MAC_APPS and provider != "claude":
            out.append(Way("mac-app", MAC_APPS[provider]))
        elif kind == "url":
            out.append(Way("url", resolve()))
        else:
            out.append(Way("cli", provider))
        return out
    out.append(Way(kind, resolve() if kind in ("url", "aumid") else resolve))
    return out


def open_app_windows(aumid: str) -> int:
    """Start (or bring forward) a Store app by its launch id; the ShellExecute
    result, over 32 on success."""
    import ctypes
    return ctypes.windll.shell32.ShellExecuteW(None, "open", f"shell:AppsFolder\\{aumid}", None, None, 1)


def cli_command(provider: str, path: Path) -> tuple[list[str], Path]:
    """The command and working folder for a CLI opened in its own console: a
    .cmd/.bat through cmd.exe, kept open; Grok from its install folder (it
    works on that folder), the others from the user's home."""
    command = ["cmd.exe", "/k", str(path)] if path.suffix.lower() in (".cmd", ".bat") else [str(path)]
    return command, (path.parent if provider == "grok" else Path.home())
