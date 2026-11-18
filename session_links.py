"""Open one agent session where the user can answer it.

A click on a task bubble goes to the session itself, not just its app:

* a session in a herdr pane: `herdr agent focus <pane>`, then the herdr
  window is brought forward;
* a Codex thread (desktop app or CLI, they share the thread store):
  `codex://threads/<thread id>`;
* a Claude Code session of the Claude desktop app:
  `claude://code/needs-input?session=local_…`, the desktop's own id, read
  from its session index (claude-code-sessions/*/*/local_*.json, which maps
  the CLI transcript id to it);
* anything else: the source's usual way of opening (the caller's fallback).

The desktop index also says whether the user has looked at a session since
its last turn ended (lastFocusedAt against lastTurnEdgeAt), which keeps an
answered or viewed session from showing as waiting.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import herdr_source

log = logging.getLogger(__name__)
INDEX_SECONDS = 10.0        # the desktop index is rescanned at most this often


def _desktop_roots() -> list[Path]:
    """Where the Claude desktop app keeps its data: the plain install, and the
    Store (MSIX) package's redirected copy."""
    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support/Claude"]
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Claude")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots += [Path(path) / "LocalCache/Roaming/Claude" for path in glob.glob(str(Path(local) / "Packages/Claude_*"))]
    return roots


class DesktopIndex:
    """CLI session id -> (desktop session id, seen since its last turn)."""

    def __init__(self, roots: Callable[[], list[Path]] = _desktop_roots):
        self.roots = roots
        self.lock = threading.Lock()
        self.sessions: dict[str, tuple[str, bool]] = {}
        self.read_at = 0.0
        self.mtimes: dict[str, float] = {}

    def lookup(self, cli_id: str) -> tuple[str, bool] | None:
        self.refresh()
        with self.lock:
            return self.sessions.get(cli_id)

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self.read_at < INDEX_SECONDS:
            return
        self.read_at = now
        found: dict[str, tuple[str, bool]] = {}
        for root in self.roots():
            for path in glob.glob(str(root / "claude-code-sessions" / "*" / "*" / "local_*.json")):
                try:
                    stamp = os.path.getmtime(path)
                    if stamp < now - 7 * 86400:
                        continue
                    with open(path, encoding="utf-8") as stream:
                        row = json.load(stream)
                except (OSError, ValueError):
                    continue
                local = str(row.get("sessionId") or "")
                if not local.startswith("local_") or row.get("isArchived"):
                    continue
                seen = float(row.get("lastFocusedAt") or 0) >= float(row.get("lastTurnEdgeAt") or 0) > 0
                for key in ("cliSessionId", "lastAssistantCliSessionId"):
                    cli = str(row.get(key) or "")
                    if cli:
                        found[cli] = (local, seen)
        with self.lock:
            self.sessions = found


DESKTOP = DesktopIndex()


def link_for(member: dict, desktop: DesktopIndex = DESKTOP) -> str | None:
    """The URL that opens this session in its desktop app, if there is one."""
    key, provider = str(member.get("key") or ""), str(member.get("provider") or "")
    if provider == "codex" and key and not key.startswith("herdr:"):
        return f"codex://threads/{key}"
    if provider == "claude" and key:
        entry = desktop.lookup(key)
        if entry:
            return f"claude://code/needs-input?session={entry[0]}"
    return None


def _start_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.startfile(url)  # type: ignore[attr-defined]


def open_session(member: dict, *, runner=subprocess.run, start_url=_start_url,
                 raise_client=herdr_source.raise_client, desktop: DesktopIndex = DESKTOP) -> str | None:
    """Open `member` (an agent dict) for the user to answer; the way used
    ("herdr", "url") or None when only the source's usual launcher is left."""
    pane = str(member.get("pane") or "")
    exe = herdr_source.find_exe() if pane else None
    if pane and exe:
        try:
            payload = herdr_source._call(exe, ["agent", "focus", pane], runner, 3)
            if not herdr_source._error(payload):
                raise_client()
                return "herdr"
            log.info("herdr could not focus pane %s: %s", pane, herdr_source._error(payload))
        except (OSError, subprocess.SubprocessError) as error:
            log.info("herdr focus failed: %s", error)
    url = link_for(member, desktop)
    if url:
        try:
            start_url(url)
            return "url"
        except OSError as error:
            log.info("could not open %s: %s", url.split("?")[0], error)
    return None
