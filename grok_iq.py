"""How close Grok 4.7 is to the smartest model, for Grok's empty middle column.

AI Stupid Level's 24 h board does not track xAI, so this column cannot use the
banner's coding score. It reads LiveBench's latest public release instead,
takes that board's weighted overall (0-100, the figure LiveBench leads with)
for every model, and shows Grok's as a share of the best one, captioned
"Grok 4.7/<best model>". A published release does not change, so a new bundle
on the homepage is the only reason to download the table again.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass

from model_iq import short_name
import app_identity

SITE = "https://livebench.ai/"
ORIGIN = "https://livebench.ai"
MODEL_ID = "grok-4.7"
LABEL = "智商"
CAPTION = "Grok 4.7"
READ_SECONDS = 3 * 3600
USER_AGENT = app_identity.USER_AGENT


@dataclass
class GrokIq:
    score: float | None = None
    top_score: float | None = None    # the best overall on the same board
    top_model: str = ""
    release: str = ""
    script: str = ""          # homepage bundle path; unchanged means the release is unchanged
    fetched_at: float = 0.0
    attempted_at: float = 0.0
    error: str = ""


def due(state: GrokIq | None, now: float) -> bool:
    """Every 3 h, and at once while the leader is unknown (a cache from
    before the comparison) so the column does not wait hours for its base."""
    if state is None or not state.top_score:
        return True
    return now - state.attempted_at >= READ_SECONDS


def score_text(score: float | None) -> str:
    """Bare number, one decimal when LiveBench shows one."""
    if score is None:
        return "—"
    text = f"{score:.1f}"
    return text[:-2] if text.endswith(".0") else text


def share(state: GrokIq | None) -> float | None:
    """Grok's overall as a percentage of the best model's."""
    if state is None or state.score is None or not state.top_score:
        return None
    return min(100.0, 100.0 * state.score / state.top_score)


def column(state: GrokIq | None) -> tuple[str, str, str, str]:
    """(label, figure, caption, unit) for the spare header slot."""
    value = share(state)
    rival = short_name(state.top_model) if state is not None and state.top_model else ""
    caption = f"{CAPTION}/{rival}" if rival else CAPTION
    return LABEL, "—" if value is None else f"{value:.0f}", caption, "" if value is None else "%"


def script_path(html: str) -> str | None:
    match = re.search(r'src="(\./static/js/main\.[A-Za-z0-9]+\.js)"', html or "")
    return match.group(1) if match else None


def latest_release(script: str) -> str | None:
    """The last id in the bundle's release list (Yt=["20261014","20261025"])."""
    match = re.search(r'Yt=\[((?:"\d{8}",?)*)\]', script or "")
    if not match:
        return None
    ids = re.findall(r'"(\d{8})"', match.group(1))
    return ids[-1] if ids else None


def parse_row(csv_text: str, model_id: str = MODEL_ID) -> dict[str, str] | None:
    reader = csv.DictReader(io.StringIO(csv_text or ""))
    wanted = (model_id or "").strip().lower()
    for row in reader:
        if (row.get("model") or "").strip().lower() == wanted:
            return {key: (value or "") for key, value in row.items()}
    return None


def leader(csv_text: str, categories: dict) -> tuple[str, float] | None:
    """(model id, overall) of the highest-scoring model on the board."""
    best = None
    for row in csv.DictReader(io.StringIO(csv_text or "")):
        score = overall({key: (value or "") for key, value in row.items()}, categories)
        if score is not None and (best is None or score > best[1]):
            best = ((row.get("model") or "").strip(), score)
    return best


def overall(row: dict[str, str], categories: dict) -> float | None:
    """LiveBench overall: weighted mean of the suites that have a score.
    A named weight is used as given; every other suite counts once.
    A blank cell is left out of both the total and the weight."""
    if not isinstance(categories, dict):
        return None
    weights = categories.get("_overall_weights")
    if not isinstance(weights, dict):
        weights = {}
    total = weight = 0.0
    found = False
    for category, columns in categories.items():
        if not isinstance(category, str) or category.startswith("_") or not isinstance(columns, list):
            continue
        values = []
        for name in columns:
            raw = row.get(str(name), "")
            if raw is None or str(raw).strip() == "":
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
        if not values:
            continue
        try:
            gain = float(weights.get(category, 1.0))
        except (TypeError, ValueError):
            gain = 1.0
        if gain <= 0:
            continue
        total += gain * (sum(values) / len(values))
        weight += gain
        found = True
    if not found or weight <= 0:
        return None
    return total / weight


def _get(url: str, timeout: float = 15.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000).decode("utf-8", "replace")


def read(state: GrokIq, now: float, get=None) -> GrokIq:
    """Refresh from the public site. A failure keeps the last score."""
    get = get or _get
    state.attempted_at = now
    previous = state.score
    try:
        html = get(ORIGIN + "/")
        path = script_path(html) or ""
        if path and path == state.script and state.score is not None and state.top_score and state.release:
            state.error = ""
            return state
        release = state.release
        if path:
            found = latest_release(get(ORIGIN + "/" + path.lstrip("./")))
            if found:
                release = found
        if not release:
            raise ValueError("no release")
        table = get(f"{ORIGIN}/data/{release}/table.csv")
        categories = json.loads(get(f"{ORIGIN}/data/{release}/categories.json"))
        row = parse_row(table)
        if row is None:
            raise ValueError("model missing")
        score = overall(row, categories)
        top = leader(table, categories)
        if score is None or top is None:
            raise ValueError("no score")
    except Exception as error:
        state.score = previous
        state.error = f"读取失败：{type(error).__name__}"
        return state
    state.score = score
    state.top_model, state.top_score = top
    state.release = release
    state.script = path or state.script
    state.fetched_at = now
    state.error = ""
    return state


def load_state(path) -> GrokIq:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return GrokIq()
        return GrokIq(**{key: value for key, value in data.items() if key in GrokIq.__dataclass_fields__})
    except (OSError, ValueError, TypeError, AttributeError):
        return GrokIq()


def save_state(path, state: GrokIq) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass
