"""Model prices for the usage figures' dollar equivalents.

The prices come from LiteLLM's public table (the one ccusage prices its
reports with), downloaded at most once a day and kept on disk with only the
token-price fields. A reply costs, at its model's list price:
uncached input + cache writes (one-hour writes at their own price) + cache
reads + output. A model with a long-context tier (prices "above_<N>k_tokens")
takes that tier's prices when the reply's prompt is longer. A model the table
does not know costs nothing here and is reported as unpriced.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import app_identity

log = logging.getLogger(__name__)
PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICES_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow" / "model_prices.json"
REFRESH_SECONDS = 86400
RETRY_SECONDS = 3600
# Where a bare model name is looked for, in this order (the plain key first).
PREFIXES = ("", "anthropic/", "openai/", "gemini/", "xai/", "vertex_ai/", "azure/", "azure_ai/", "bedrock/",
            "openrouter/", "kyma/")
KEPT = re.compile(r"^(input_cost_per_token|output_cost_per_token|cache_creation_input_token_cost|"
                  r"cache_read_input_token_cost)(_above_1hr)?(_above_\d+k_tokens)?$")
TIER = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")
# xAI reports a request's cost in ticks of 1e-10 US dollars.
USD_PER_TICK = 1e-10


def slim(table: Any) -> dict[str, dict[str, float]]:
    """The token-price fields of each model; everything else is dropped."""
    out = {}
    if not isinstance(table, dict):
        return out
    for name, entry in table.items():
        if not isinstance(entry, dict) or not isinstance(name, str):
            continue
        fields = {key: float(value) for key, value in entry.items()
                  if KEPT.match(key) and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0}
        if "input_cost_per_token" in fields or "output_cost_per_token" in fields:
            out[name] = fields
    return out


class PriceTable:
    def __init__(self, table: dict | None = None):
        self.table: dict[str, dict[str, float]] = table or {}
        self.found: dict[str, dict | None] = {}
        self.lock = threading.Lock()
        self.fetched_at = 0.0
        self.attempted_at = 0.0

    def __bool__(self) -> bool:
        return bool(self.table)

    # ----- the table ----------------------------------------------------------

    def load(self, path: Path = PRICES_PATH) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            table = slim(payload.get("models"))
            if table:
                with self.lock:
                    self.table, self.found = table, {}
                    self.fetched_at = float(payload.get("fetched_at") or 0.0)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def refresh(self, now: float | None = None, path: Path = PRICES_PATH, get=None) -> bool:
        """Download the table when the one on disk is a day old (an hour
        after a failure); True when a new one was taken."""
        now = time.time() if now is None else now
        if (self.fetched_at and now - self.fetched_at < REFRESH_SECONDS
                or self.attempted_at and now - self.attempted_at < RETRY_SECONDS):
            return False
        self.attempted_at = now
        try:
            if get is None:
                request = urllib.request.Request(PRICES_URL, headers={"User-Agent": app_identity.USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read(20_000_000)
            else:
                raw = get(PRICES_URL)
            table = slim(json.loads(raw))
            if len(table) < 100:
                raise ValueError("price table too small")
        except Exception as error:
            log.info("model prices unavailable: %s", type(error).__name__)
            return False
        with self.lock:
            self.table, self.found, self.fetched_at = table, {}, now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"fetched_at": now, "source": PRICES_URL, "models": table}), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass
        log.info("model prices: %d models", len(table))
        return True

    # ----- prices -------------------------------------------------------------

    def price(self, model: str) -> dict[str, float] | None:
        """The table's entry for a model as the agents name it: the plain key,
        then with a provider prefix, then with a trailing part dropped
        ("grok-4.7-build" -> "grok-4.7")."""
        name = (model or "").strip().lower()
        if not name:
            return None
        with self.lock:
            if name in self.found:
                return self.found[name]
            table = self.table
        lowered = {key.lower(): key for key in table} if table else {}
        found = None
        candidate = name.split("/")[-1]
        while candidate and found is None:
            for prefix in PREFIXES:
                key = lowered.get(prefix + candidate)
                if key is not None:
                    found = table[key]
                    break
            candidate = candidate.rsplit("-", 1)[0] if "-" in candidate else ""
            if candidate and not re.search(r"\d", candidate):
                break                   # down to the family name: too loose to price
        with self.lock:
            self.found[name] = found
        return found

    def cost(self, model: str, uncached: float, output: float, cache_write: float = 0.0, cache_read: float = 0.0,
             cache_write_1h: float = 0.0) -> float | None:
        """US dollars at the model's list price; None when it is not in the table."""
        entry = self.price(model)
        if entry is None:
            return None
        prompt = uncached + cache_write + cache_write_1h + cache_read
        tier = ""
        for key in entry:
            match = TIER.match(key)
            if match and prompt > int(match.group(1)) * 1000 and (not tier or int(match.group(1)) > int(tier)):
                tier = match.group(1)
        def rate(name: str, fallback: float = 0.0) -> float:
            if tier and f"{name}_above_{tier}k_tokens" in entry:
                return entry[f"{name}_above_{tier}k_tokens"]
            return entry.get(name, fallback)
        base = rate("input_cost_per_token")
        write = rate("cache_creation_input_token_cost", base)
        write_1h = entry.get(f"cache_creation_input_token_cost_above_1hr_above_{tier}k_tokens" if tier else "",
                             entry.get("cache_creation_input_token_cost_above_1hr", write))
        read = rate("cache_read_input_token_cost", base)
        return (uncached * base + cache_write * write + cache_write_1h * write_1h + cache_read * read
                + output * rate("output_cost_per_token"))


TABLE = PriceTable()
TABLE.load()


def money(usd: float | None) -> str:
    """"$0.42", "$12.3", "$123", "$1.2K"; empty for nothing."""
    if usd is None or usd <= 0:
        return ""
    if usd < 10:
        return f"${usd:.2f}"
    if usd < 100:
        return f"${usd:.1f}"
    if usd < 10_000:
        return f"${usd:,.0f}"
    return f"${usd / 1000:.1f}K"
