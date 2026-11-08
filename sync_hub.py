"""Multi-device sync through the hub on the Instabox box (sync_server.py).

Each device puts one payload of its own figures and reads everyone's back:
- quota readings, which belong to the account and not the device, so the
  newest reading from any device is taken (only ever newer than the one
  shown; a failed read elsewhere never replaces a good one here);
- this device's hourly output totals, which the others add to their usage
  insights as another device's work;
- the weekly ledger, merged week by week.

Only numbers leave the machine: no conversation text, paths or credentials.
The hub sits behind Instabox's gate for private ports ("Authorization:
Bearer <link token>") and checks its own token ("X-Sync-Token"). Settings
come from sync.json in the data folder, or CODEX_QUOTA_SYNC_URL /
_SYNC_LINK_TOKEN / _SYNC_TOKEN / _SYNC_DEVICE. While the box is paused or
cooling down every call fails; the loop backs off and both sides go on
locally.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from quota_sources import QuotaSnapshot

log = logging.getLogger("CodexQuotaTray")
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray"
CONFIG_PATH = DATA_DIR / "sync.json"
SYNC_EVERY = 120          # seconds between rounds
HEARTBEAT = 600           # an unchanged payload is still sent this often
BACKOFF = (120, 300, 600)  # after 1, 2, 3+ failed rounds
TIMEOUT = 60              # a paused box can take ~45 s to wake on a request
USAGE_DAYS = 8
SNAPSHOT_FIELDS = {field.name for field in dataclasses.fields(QuotaSnapshot)}


def load_config(path: Path = CONFIG_PATH) -> dict | None:
    """{"url", "link_token", "token", "device"}, or None when sync is not set up."""
    config: dict = {}
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        config = {}
    for key, name in (("url", "URL"), ("link_token", "LINK_TOKEN"), ("token", "TOKEN"), ("device", "DEVICE")):
        value = os.environ.get(f"CODEX_QUOTA_SYNC_{name}")
        if value:
            config[key] = value
    if not config.get("url") or not config.get("token"):
        return None
    config["url"] = str(config["url"]).rstrip("/")
    config["device"] = device_id(config.get("device") or "")
    return config


def device_id(name: str = "") -> str:
    """A stable id the hub accepts: the configured name, else this computer's."""
    raw = name or f"{'mac' if sys.platform == 'darwin' else 'win'}-{socket.gethostname()}"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.")[:64]
    return cleaned or "device"


def device_label() -> str:
    kind = "Mac" if sys.platform == "darwin" else ("Windows" if sys.platform == "win32" else "Linux")
    return f"{kind} · {socket.gethostname()}"


class HubClient:
    def __init__(self, config: dict, opener: Callable = urllib.request.urlopen):
        self.config = config
        self.opener = opener

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"X-Sync-Token": self.config["token"], "Accept": "application/json"}
        if self.config.get("link_token"):
            headers["Authorization"] = f"Bearer {self.config['link_token']}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.config["url"] + path, data=data, headers=headers, method=method)
        with self.opener(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def put(self, device: str, payload: dict) -> None:
        self._call("PUT", f"/v1/devices/{device}", payload)

    def get_all(self) -> dict[str, dict]:
        return self._call("GET", "/v1/devices").get("devices", {})


def snapshot_payload(snapshots: dict[str, QuotaSnapshot | None]) -> dict[str, dict]:
    """The readings worth sharing: real ones, not failures or placeholders."""
    return {provider: snapshot.to_dict() for provider, snapshot in snapshots.items()
            if snapshot is not None and not snapshot.error and snapshot.updated_at and snapshot.used_percent is not None}


def snapshot_from(fields: Any) -> QuotaSnapshot | None:
    """A QuotaSnapshot from another device's payload, or None when malformed."""
    if not isinstance(fields, dict):
        return None
    try:
        values = {key: value for key, value in fields.items() if key in SNAPSHOT_FIELDS}
        if fields.get("credits_unlimited") is True:
            values["credits_balance"] = float("inf")
        snapshot = QuotaSnapshot(**values)
    except TypeError:
        return None
    if not isinstance(snapshot.updated_at, (int, float)) or not snapshot.provider:
        return None
    return snapshot


def merge_snapshots(payloads: dict[str, dict], me: str, current: dict[str, QuotaSnapshot | None],
                    accept: Callable[[str, QuotaSnapshot, str], bool]) -> list[tuple[str, QuotaSnapshot]]:
    """Offer every other device's readings that are newer than the one shown
    here (`current`, by updated_at) to `accept` (provider, snapshot, source);
    returns the ones it took. A reading this device passed on comes back no
    newer, so nothing bounces between devices."""
    taken = []
    for device, record in payloads.items():
        if device == me or not isinstance(record, dict):
            continue
        payload = record.get("payload") or {}
        for provider, fields in (payload.get("snapshots") or {}).items():
            if provider not in current:
                continue
            snapshot = snapshot_from(fields)
            if snapshot is None or snapshot.error or snapshot.provider != provider:
                continue
            shown = current.get(provider)
            if shown is not None and (snapshot.updated_at or 0) <= (shown.updated_at or 0):
                continue
            if accept(provider, snapshot, f"sync:{device}"):
                taken.append((provider, snapshot))
                current[provider] = snapshot
    return taken


class SyncLoop:
    """One round every SYNC_EVERY seconds: put this device's payload (when it
    changed, or every HEARTBEAT), then read everyone's and hand them to
    `merge`. Failures back off; status() says how it is going."""

    def __init__(self, client: HubClient, device: str, collect: Callable[[float], dict],
                 merge: Callable[[dict[str, dict], float], None]):
        self.client, self.device = client, device
        self.collect, self.merge = collect, merge
        self.sent_signature = ""
        self.sent_at = 0.0
        self.failures = 0
        self.last_ok = 0.0
        self.last_error = ""
        self.devices: dict[str, str] = {}       # device -> label, from the last read
        self.next_at = 0.0
        self.lock = threading.Lock()

    def tick(self, now: float) -> bool:
        """One round; True when it reached the hub."""
        if now < self.next_at:
            return False
        try:
            payload = self.collect(now)
            signature = json.dumps({key: value for key, value in payload.items() if key != "sent_at"}, sort_keys=True, default=str)
            if signature != self.sent_signature or now - self.sent_at >= HEARTBEAT:
                self.client.put(self.device, payload)
                self.sent_signature, self.sent_at = signature, now
            payloads = self.client.get_all()
            self.merge(payloads, now)
        except (OSError, ValueError, urllib.error.URLError) as error:
            with self.lock:
                self.failures += 1
                self.last_error = _reason(error)
                self.next_at = now + BACKOFF[min(self.failures, len(BACKOFF)) - 1]
            if self.failures in (1, 4) or self.failures % 20 == 0:
                log.info("sync: hub unreachable (%s); next try in %.0f s", self.last_error, self.next_at - now)
            return False
        with self.lock:
            if self.failures:
                log.info("sync: hub reachable again after %d failed rounds", self.failures)
            self.failures, self.last_error, self.last_ok = 0, "", now
            self.devices = {device: str(((record.get("payload") or {}).get("label")) or device)
                            for device, record in payloads.items() if isinstance(record, dict)}
            self.next_at = now + SYNC_EVERY
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick(time.time())
            except Exception:
                log.exception("sync round failed")
            stop.wait(10)

    def status(self, now: float | None = None) -> str:
        now = now or time.time()
        with self.lock:
            others = [label for device, label in self.devices.items() if device != self.device]
            if self.failures:
                return f"暂时连不上同步服务器（{self.last_error}），{max(0, round((self.next_at - now) / 60))} 分钟后重试"
            if not self.last_ok:
                return "正在连接同步服务器"
            when = time.strftime("%H:%M", time.localtime(self.last_ok))
            return f"{when} 已同步" + (f"；另有 {len(others)} 台设备：{'、'.join(others)}" if others else "；暂无其他设备")


def _reason(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return {401: "令牌无效", 502: "服务器上的同步服务未运行", 503: "服务器暂停或冷却中"}.get(error.code, f"HTTP {error.code}")
    if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in str(error):
        return "连接超时，服务器可能在冷却"
    return type(error).__name__
