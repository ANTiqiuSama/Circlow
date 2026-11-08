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
come from sync.json in the data folder, or CIRCLOW_SYNC_URL /
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

log = logging.getLogger("Circlow")
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow"
CONFIG_PATH = DATA_DIR / "sync.json"
SYNC_EVERY = 120          # seconds between rounds
HEARTBEAT = 600           # an unchanged payload is still sent this often
BACKOFF = (120, 300, 600)  # after 1, 2, 3+ failed rounds
TIMEOUT = 60              # a paused box can take ~45 s to wake on a request
USAGE_DAYS = 8
# Live TPS: while any device is working, every LIVE_ACTIVE seconds; otherwise
# every LIVE_IDLE. A device not heard from for LIVE_FRESH seconds (by the
# hub's clock) no longer counts. A reply's speed is shown for SAMPLE_AGE
# seconds, as tps_sources does for local replies.
LIVE_ACTIVE = 3
LIVE_IDLE = 30
LIVE_FRESH = 90
LIVE_TIMEOUT = 15
SAMPLE_AGE = 300
SNAPSHOT_FIELDS = {field.name for field in dataclasses.fields(QuotaSnapshot)}


def load_config(path: Path = CONFIG_PATH) -> dict | None:
    """{"url", "link_token", "token", "device"}, or None when sync is not set up."""
    config: dict = {}
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        config = {}
    for key, name in (("url", "URL"), ("link_token", "LINK_TOKEN"), ("token", "TOKEN"), ("device", "DEVICE")):
        value = os.environ.get(f"CIRCLOW_SYNC_{name}")
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

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT) -> dict:
        headers = {"X-Sync-Token": self.config["token"], "Accept": "application/json"}
        if self.config.get("link_token"):
            headers["Authorization"] = f"Bearer {self.config['link_token']}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.config["url"] + path, data=data, headers=headers, method=method)
        with self.opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def put(self, device: str, payload: dict) -> None:
        self._call("PUT", f"/v1/devices/{device}", payload)

    def get_all(self) -> dict[str, dict]:
        return self._call("GET", "/v1/devices").get("devices", {})

    def put_live(self, device: str, payload: dict) -> None:
        self._call("PUT", f"/v1/live/{device}", payload, timeout=LIVE_TIMEOUT)

    def get_live(self) -> tuple[dict[str, dict], float]:
        """Every device's live TPS and the hub's clock."""
        answer = self._call("GET", "/v1/live", timeout=LIVE_TIMEOUT)
        return answer.get("live", {}), float(answer.get("now") or time.time())


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


# ----- live TPS: the same figure on every device -----------------------------------------

def live_payload(performance: dict, now: float, label: str = "") -> dict:
    """This device's own TPS per source, for the hub's live channel. Ages, not
    timestamps, so the devices' clocks never have to agree."""
    providers = {}
    for provider, data in (performance.get("providers") or {}).items():
        sample = data.get("last_sample_at")
        age = round(now - sample, 2) if sample else None
        active = int(data.get("active_sessions") or 0)
        tokens = int(data.get("output_tokens_60") or 0)
        if active or tokens or (age is not None and age <= SAMPLE_AGE):
            providers[provider] = {"active": active, "tps": data.get("display_tps"), "age": age,
                                   "model": data.get("display_model"), "tokens_60": tokens}
    return {"label": label or device_label(), "providers": providers}


def combine(performance: dict, live: dict[str, dict], hub_now: float, me: str, now: float) -> dict:
    """`performance` (this device's, from TpsMonitor.poll) with the other
    devices' live TPS folded in, so every device shows the same figures:

    - the TPS shown is the total: the speeds of every device generating right
      now (working, with a reply in the last SAMPLE_AGE seconds) add up; with
      none generating, the most recent reply's, on whichever device it ran;
    - the 60 s output and the number of working sessions add up;
    - each source keeps this device's own figure as local_tps / local_active
      / local_output_tps_60, and names the other working devices in `devices`.

    Every device applies the same rule to the same numbers, so they agree up
    to one live round (LIVE_ACTIVE seconds)."""
    others = []
    for device, record in (live or {}).items():
        if device == me or not isinstance(record, dict):
            continue
        late = hub_now - float(record.get("received_at") or 0)
        payload = record.get("payload") or {}
        if -5 <= late <= LIVE_FRESH:                  # a few seconds of jitter between hub reads
            others.append((str(payload.get("label") or device), max(0.0, late), payload.get("providers") or {}))
    merged = dict(performance)
    providers = {}
    total = 0
    for provider, data in (performance.get("providers") or {}).items():
        row = dict(data)
        local_tps, local_active, local_tokens = data.get("display_tps"), int(data.get("active_sessions") or 0), int(data.get("output_tokens_60") or 0)
        sample = data.get("last_sample_at")
        best = (now - sample, local_tps, data.get("display_model")) if sample and local_tps is not None else None
        running = [best] if best is not None and local_active and best[0] <= SAMPLE_AGE else []
        active, tokens, devices = local_active, local_tokens, []
        for label, late, rows in others:
            remote = rows.get(provider)
            if not isinstance(remote, dict):
                continue
            try:
                remote_active, remote_tokens = int(remote.get("active") or 0), int(remote.get("tokens_60") or 0)
                remote_tps = None if remote.get("tps") is None else float(remote["tps"])
                age = None if remote.get("age") is None else float(remote["age"]) + late
            except (TypeError, ValueError):
                continue
            active += remote_active
            tokens += remote_tokens
            if remote_tps is not None and age is not None and age <= SAMPLE_AGE:
                if best is None or age < best[0]:
                    best = (age, remote_tps, remote.get("model"))
                if remote_active:
                    running.append((age, remote_tps, remote.get("model")))
            if remote_active or remote_tokens or (remote_tps is not None and age is not None and age <= SAMPLE_AGE):
                devices.append({"label": label, "tps": remote_tps, "active": remote_active})
        row.update(local_tps=local_tps, local_active=local_active, local_output_tps_60=data.get("output_tps_60", 0.0),
                   devices=devices, active_sessions=active, output_tokens_60=tokens, output_tps_60=round(tokens / 60, 2))
        if running:
            # Several devices generating at once: their speeds add up.
            age, _tps, model = min(running, key=lambda item: item[0])
            tps = round(sum(item[1] for item in running), 1)
            row.update(display_tps=tps, generation_tps=tps, display_model=model or data.get("display_model"),
                       last_sample_at=now - age)
        elif best is not None:
            age, tps, model = best
            row.update(display_tps=tps, generation_tps=tps, display_model=model or data.get("display_model"),
                       last_sample_at=now - age)
        providers[provider] = row
        total += tokens
    merged["providers"] = providers
    merged["local_total_output_tps_60"] = performance.get("total_output_tps_60", 0.0)
    merged["total_output_tps_60"] = round(total / 60, 2)
    merged["devices_seen"] = len(others)
    return merged


class LiveLoop:
    """Swaps live TPS with the other devices: every LIVE_ACTIVE seconds while
    any of them (or this one) is working, every LIVE_IDLE otherwise. view()
    gives the last read, adjusted to the hub's clock now."""

    def __init__(self, client: HubClient, device: str, collect: Callable[[float], dict]):
        self.client, self.device, self.collect = client, device, collect
        self.live: dict[str, dict] = {}
        self.hub_now = 0.0
        self.read_at = 0.0
        self.failures = 0
        self.next_at = 0.0
        self.lock = threading.Lock()

    def busy(self, payload: dict) -> bool:
        if any(row.get("active") for row in (payload.get("providers") or {}).values()):
            return True
        with self.lock:
            late = self.hub_now
            return any(device != self.device and late - float(record.get("received_at") or 0) <= LIVE_FRESH
                       and any(row.get("active") for row in ((record.get("payload") or {}).get("providers") or {}).values()
                               if isinstance(row, dict))
                       for device, record in self.live.items() if isinstance(record, dict))

    def tick(self, now: float) -> bool:
        if now < self.next_at:
            return False
        try:
            payload = self.collect(now)
            self.client.put_live(self.device, payload)
            live, hub_now = self.client.get_live()
        except (OSError, ValueError, urllib.error.URLError):
            self.failures += 1
            self.next_at = now + BACKOFF[min(self.failures, len(BACKOFF)) - 1]
            return False
        with self.lock:
            self.live, self.hub_now, self.read_at = live, hub_now, now
        self.failures = 0
        self.next_at = now + (LIVE_ACTIVE if self.busy(payload) else LIVE_IDLE)
        return True

    def wake(self) -> None:
        """This device started working: swap at once rather than at the idle pace."""
        self.next_at = 0.0

    def view(self, now: float) -> tuple[dict[str, dict], float]:
        with self.lock:
            if not self.read_at:
                return {}, now
            return self.live, self.hub_now + (now - self.read_at)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick(time.time())
            except Exception:
                log.exception("live TPS round failed")
            stop.wait(0.5)


def _reason(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return {401: "令牌无效", 502: "服务器上的同步服务未运行", 503: "服务器暂停或冷却中"}.get(error.code, f"HTTP {error.code}")
    if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in str(error):
        return "连接超时，服务器可能在冷却"
    return type(error).__name__
