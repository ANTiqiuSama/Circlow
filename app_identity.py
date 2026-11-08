"""The app's names, and the move from its old name.

环流 · Circlow (Circle + Flow) was called CodexQuotaTray. Everything that
carried the old name moves over on its own the first time the new build
starts: the data folder (migrate_data_dir), the CODEX_QUOTA_* environment
settings (still read, as CIRCLOW_*), the Windows startup entry and the
macOS LaunchAgent (in tray_app / settings_mac), and the Claude Code
status-line receiver (claude_status).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Circlow"                 # files, folders, the logger, mutexes
DISPLAY_NAME = "环流"                 # what people read
FULL_NAME = f"{DISPLAY_NAME} {APP_NAME}"
BUNDLE_ID = "com.antiqiusama.circlow"
REPO_URL = "https://github.com/ANTiqiuSama/Circlow"
USER_AGENT = f"{APP_NAME} (+{REPO_URL})"
ENV_PREFIX = "CIRCLOW_"

LEGACY_NAME = "CodexQuotaTray"
LEGACY_BUNDLE_ID = "com.antiqiusama.codexquotatray"
LEGACY_ENV_PREFIX = "CODEX_QUOTA_"

log = logging.getLogger(APP_NAME)


def local_root() -> Path:
    """Where per-user app data lives: %LOCALAPPDATA% on Windows; on macOS
    mac_menu points LOCALAPPDATA at ~/Library/Application Support."""
    default = Path.home() / ("Library/Application Support" if sys.platform == "darwin" else "AppData/Local")
    return Path(os.environ.get("LOCALAPPDATA") or default)


def data_dir() -> Path:
    return local_root() / APP_NAME


def adopt_legacy_env(environ=os.environ) -> list[str]:
    """CODEX_QUOTA_X set and CIRCLOW_X not: use it as CIRCLOW_X. Returns
    the names adopted."""
    adopted = []
    for name, value in list(environ.items()):
        if name.startswith(LEGACY_ENV_PREFIX):
            new = ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):]
            if new not in environ:
                environ[new] = value
                adopted.append(new)
    return adopted


def migrate_data_dir(root: Path | None = None) -> str:
    """Move the old data folder to the new name, once. A rename keeps
    everything in place (a junction or symlink is renamed itself and still
    points where it did); when the old folder is in use by an old copy
    still running, it is copied instead and left behind. Returns what was
    done: "", "renamed" or "copied"."""
    root = root or local_root()
    old, new = root / LEGACY_NAME, root / APP_NAME
    if new.exists() or new.is_symlink() or not (old.exists() or old.is_symlink()):
        return ""
    try:
        os.rename(old, new)
        return "renamed"
    except OSError:
        pass
    try:
        shutil.copytree(old, new, symlinks=True, ignore=shutil.ignore_patterns("*.tmp", "_MEI*"))
        return "copied"
    except OSError:
        log.warning("could not move %s to %s", old, new, exc_info=True)
        return ""


adopt_legacy_env()
