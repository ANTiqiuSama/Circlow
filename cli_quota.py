"""Bound native CLI quota reads, including PTY construction, in disposable processes."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

import psutil

import process_jail
from cli_usage import CLAUDE_ARGS, CLAUDE_PROBE, GROK_PROBE, parse_claude_usage, parse_grok_usage, run_temporary_usage

GROK_ARGS = ["--minimal", "--no-alt-screen", "--no-memory", "--no-subagents", "--disable-web-search"]


def no_quota_reason(executable: str) -> str | None:
    """Use the noninteractive status first; older CLIs can still use /usage."""
    try:
        result = process_jail.run([executable, "auth", "status"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(status, dict):
        return None
    if status.get("loggedIn") is False:
        return "Claude Code 未登录，未找到订阅额度"
    if str(status.get("authMethod", "")).lower() in {"api_key", "api-key"}:
        return "API Key 计费，未找到订阅额度"
    if str(status.get("subscriptionType", "")).lower() in {"free", "none"}:
        return "未找到订阅额度（当前 Claude 账号无订阅）"
    return None


def stop_worker(process: subprocess.Popen) -> None:
    """End a disposable worker and everything it started. Its job holds the
    whole tree (PTY host, CLI, the CLI's MCP servers), including processes
    whose parent already exited; the snapshot below only covers a worker that
    could not be put in a job. Never touches an existing client session."""
    job = getattr(process, "job", None)
    if job is not None:
        job.close()
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    process.kill()
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    process.wait(timeout=5)


def read_usage(executable: str, provider: str = "claude", raw: dict | None = None) -> dict:
    if provider not in ("claude", "grok"):
        raise ValueError("Unsupported CLI quota provider")
    reason = no_quota_reason(executable) if provider == "claude" else None
    if reason:
        raise ValueError(reason)
    # Resolve migrated data junctions before creating a child directory (also
    # avoids MSIX Python's LocalAppData redirection across a reparse point).
    folder = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow/work").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{provider}-quota-", dir=folder) as temporary:
        destination = Path(temporary) / "result.json"
        frozen = getattr(sys, "frozen", False)
        command = ([sys.executable, f"--{provider}-quota-worker"] if frozen
                   else [sys.executable, "-X", "utf8", str(Path(__file__).resolve())])
        command += [executable, str(destination)]
        if not frozen:
            command.append(provider)
        process = process_jail.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timeout = 45 if provider == "claude" else 35
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_worker(process)
            label = "Claude Code" if provider == "claude" else "Grok"
            raise TimeoutError(f"{label} 额度读取在 {timeout} 秒内未完成（含隐藏终端初始化）") from None
        # The worker finished; anything it left behind (the CLI's MCP servers) ends with its job.
        job = getattr(process, "job", None)
        if job is not None:
            job.close()
        result = json.loads(destination.read_text(encoding="utf-8"))
        if raw is not None:
            raw.update(result.get("cli", {}))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["quota"]


def worker(executable: str, destination: str, provider: str = "claude") -> int:
    try:
        if provider == "grok":
            text = run_temporary_usage(executable, GROK_ARGS, timeout=25, probe=GROK_PROBE)
            result = {"quota": parse_grok_usage(text), "cli": {}}
            for key, label in (("weekly_limit", "Weekly limit"), ("next_reset", "Next reset")):
                match = re.search(rf"{label}\s*[:：][^\r\n]*", text, re.I)
                if match:
                    result["cli"][key] = match.group(0).strip()
        elif provider == "claude":
            text = run_temporary_usage(executable, CLAUDE_ARGS, timeout=30, probe=CLAUDE_PROBE)
            result = {"quota": parse_claude_usage(text)}
        else:
            raise ValueError("Unsupported CLI quota provider")
        code = 0
    except Exception as error:
        result, code = {"error": f"{type(error).__name__}: {error}"}, 1
    # Only parsed quota numbers or an error; never persist the terminal screen.
    Path(destination).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(worker(*sys.argv[1:]))
