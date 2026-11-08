"""The sync hub that runs on the Instabox box: each device puts one JSON
payload of its own figures (quota readings, hourly output totals, weekly
ledger) and reads everyone's back. Standard library only.

    PUT    /v1/devices/<device>   body: the device's payload (JSON object, ≤ 2 MB)
    GET    /v1/devices            {"devices": {id: {"received_at", "payload"}}}
    DELETE /v1/devices/<device>
    GET    /v1/health             {"ok": true}

Every call but health needs "X-Sync-Token: <token>", the token being the
contents of SYNC_TOKEN_FILE. (The Authorization header belongs to Instabox's
gate in front of a private port: "Bearer <link token>".) Payloads live as one file per device in
SYNC_DIR; devices silent for 30 days are left out of listings.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("SYNC_DIR", "/data/workspace/quota-sync/devices"))
TOKEN = Path(os.environ.get("SYNC_TOKEN_FILE", "/data/workspace/quota-sync/token")).read_text(encoding="utf-8").strip()
MAX_BODY = 2 * 1024 * 1024
FORGET_SECONDS = 30 * 86400
DEVICE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "quota-sync/1"

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Sync-Token", "").strip(), TOKEN)

    def _route(self) -> tuple[str, str | None]:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/v1/health":
            return "health", None
        if path == "/v1/devices":
            return "devices", None
        if path.startswith("/v1/devices/"):
            device = path[len("/v1/devices/"):]
            return ("device", device) if DEVICE.match(device) else ("bad", None)
        return "missing", None

    def do_GET(self) -> None:
        route, _ = self._route()
        if route == "health":
            return self._send(200, {"ok": True})
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if route != "devices":
            return self._send(404, {"error": "not found"})
        now = time.time()
        devices = {}
        for path in ROOT.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if now - float(record.get("received_at", 0)) <= FORGET_SECONDS:
                devices[path.stem] = record
        self._send(200, {"devices": devices, "now": now})

    def do_PUT(self) -> None:
        route, device = self._route()
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if route != "device":
            return self._send(404 if route == "missing" else 400, {"error": "bad device"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "body size"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "not json"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "not an object"})
        ROOT.mkdir(parents=True, exist_ok=True)
        record = {"received_at": time.time(), "payload": payload}
        temporary = ROOT / f".{device}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, ROOT / f"{device}.json")
        self._send(200, {"ok": True, "received_at": record["received_at"]})

    def do_DELETE(self) -> None:
        route, device = self._route()
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if route != "device":
            return self._send(404, {"error": "bad device"})
        try:
            (ROOT / f"{device}.json").unlink()
        except FileNotFoundError:
            pass
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args) -> None:       # no request lines (they carry device names) in the journal
        return


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8090"))), Handler).serve_forever()
