"""Cost Chart backend — daily AI spend + Vectorizer savings, mounted at /api/plugins/costchart/.

Reads every Hermes profile's session ledger (``state.db``, opened read-only) and
attributes token spend to the four provider lanes:

- ``xiaomi``      — MiMo token-plan sessions (``token-plan-sgp.xiaomimimo.com``)
- ``opencode-go`` — muse-spark sessions via OpenCode Zen Go (``opencode.ai/zen/go``)
- ``meta``        — meta-ai / muse-spark sessions (``api.meta.ai``)
- ``local``       — local LM Studio sessions (``localhost:1234`` / ``127.0.0.1:1234``)
- ``other``       — anything else (kept visible so totals reconcile)

Daily attribution: the ledger stores totals per SESSION, and long-lived sessions
span days. Each session's totals are spread across its active days pro-rata by
that day's message weight (``messages.token_count``, falling back to message
count). Sessions with no messages fall back to their start day (MYT, UTC+8).

Vectorizer savings (estimate, formula exposed in the payload): each retrieval
call replaces a context-heavy knowledge read. ``avoided_per_call`` is measured
from our own ledgers — the average tool-result size of file/web knowledge reads
(``read_file`` / ``search_files`` / ``web_extract``) minus the average
Vectorizer result size — and priced at the fleet's blended non-cache token rate.
"""

from __future__ import annotations

import calendar
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter()

_TZ_OFFSET = 8 * 3600  # MYT (UTC+8) — the fleet's operating timezone
_CACHE_TTL = 120.0  # seconds — the page polls; the ledger scan is heavier than a poll

# Knowledge reads a retrieval replaces (context that would have entered the prompt).
_KNOWLEDGE_TOOLS = ("session_search", "read_file", "skill_view", "web_extract", "search_files")

# Stated assumption (kept as one adjustable constant): one retrieval replaces a
# search probe AND a source read — the standard non-retrieval lookup sequence.
REPLACED_PER_LOOKUP = 2.0

# muse-spark-1.3-contributor list rates ($/1M tokens) — the Contributor tier
# (training rights in exchange for the discount; the standard muse-spark-1.3
# ID is $1.25/$4.25). Used as the cost fallback for the spark lanes (meta and
# opencode-go) when the ledger carries no price for a session (cost_status
# unknown / estimated_cost_usd 0): the ledger estimate wins whenever present
# and priced, list rates fill the gap so the chart shows reality, never $0.
# Sources: Baseer model listing, explainx.ai cost page, MyClaw.ai 1.3 pricing
# table (Oct 2026). OpenCode Zen Go passes through the same model price — no
# separate zen list rate is documented, so the same rates apply there until
# one is published.
_META_RATES_PER_M = {"input": 0.10, "output": 0.20, "cached": 0.002}
_OPENCODE_GO_RATES_PER_M = dict(_META_RATES_PER_M)


def _list_rate_cost(in_tok: float, out_tok: float, cache_tok: float,
                   rates: Dict[str, float]) -> float:
    """Cost in USD for token counts at ``rates`` ($/1M tokens)."""
    return (in_tok * rates["input"]
            + cache_tok * rates["cached"]
            + out_tok * rates["output"]) / 1_000_000.0

_cache: Dict[str, Any] = {}
_cache_at = 0.0
_cache_lock = threading.Lock()


# --- Discovery ----------------------------------------------------------------

def _hermes_root() -> Path:
    """Hermes root (the parent of ``plugins/`` and ``profiles/``).

    Prefer the runtime helper so profile-scoped processes resolve correctly;
    fall back to the install path (``<root>/plugins/costchart/dashboard/``).
    """
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore
        return Path(get_default_hermes_root())
    except Exception:
        return Path(__file__).resolve().parents[3]


def _ledger_paths() -> List[Path]:
    root = _hermes_root()
    paths: List[Path] = []
    profiles = root / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            db = p / "state.db"
            if db.is_file():
                paths.append(db)
    root_db = root / "state.db"
    if root_db.is_file():
        paths.append(root_db)
    return paths


# --- Attribution --------------------------------------------------------------

def _provider(billing_provider: Optional[str], base_url: Optional[str], model: Optional[str]) -> str:
    base = (base_url or "").lower()
    prov = (billing_provider or "").lower()
    mod = (model or "").lower()
    if "xiaomimimo" in base or prov == "xiaomi":
        return "xiaomi"
    if "opencode.ai/zen/go" in base or prov == "opencode-go":
        return "opencode-go"
    if "meta.ai" in base or prov == "meta-ai":
        return "meta"
    if "127.0.0.1:1234" in base or "localhost:1234" in base or prov in ("custom", "lmstudio"):
        return "local"
    if "muse" in mod:
        return "meta"
    if "qwen" in mod:
        return "local"
    if mod.startswith("mimo"):
        return "xiaomi"
    return "other"


def _day(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(float(epoch) + _TZ_OFFSET))
    except (TypeError, ValueError):
        return None


# --- Aggregation --------------------------------------------------------------

def _empty_series(days: List[str]) -> Dict[str, List[float]]:
    return {k: [0.0] * len(days) for k in
            ("cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "calls")}


def _scan_db(db: Path, buckets: Dict[str, Dict[str, List[float]]], day_index: Dict[str, int],
             vec_calls: Dict[str, float], tool_sizes: Dict[str, List[float]],
             totals: Dict[str, float]) -> None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("costchart: cannot open %s: %s", db, exc)
        return
    try:
        cur = con.cursor()
        sessions = cur.execute(
            "SELECT id, billing_provider, billing_base_url, model,"
            "       input_tokens, output_tokens, cache_read_tokens, api_call_count,"
            "       estimated_cost_usd, started_at, last_activity_at"
            " FROM sessions"
        ).fetchall()

        # Per-session, per-day message weight (token_count when present; tool
        # rows carry none, so fall back to content size / 4 as a token estimate).
        weights: Dict[str, Dict[str, float]] = {}
        for sid, d, w in cur.execute(
            "SELECT session_id, date(timestamp + ? , 'unixepoch') AS d,"
            "       SUM(CASE WHEN COALESCE(token_count,0) > 0 THEN token_count"
            "               ELSE LENGTH(COALESCE(content,'')) / 4.0 END) AS w"
            " FROM messages WHERE timestamp IS NOT NULL GROUP BY session_id, d",
            (_TZ_OFFSET,),
        ):
            if d:
                weights.setdefault(sid, {})[d] = float(w or 0)

        for (sid, prov, base, model, in_tok, out_tok, cache_tok, calls,
             cost, started, last) in sessions:
            lane = _provider(prov, base, model)
            series = buckets.setdefault(lane, _empty_series(list(day_index)))
            vals = (float(in_tok or 0), float(out_tok or 0), float(cache_tok or 0),
                    float(calls or 0), float(cost or 0))
            # The spark lanes (meta, opencode-go) usually arrive unpriced
            # (cost_status unknown, est $0) — price those sessions from the
            # public list rates so the chart shows reality. A ledger estimate
            # wins whenever present and priced (est > 0).
            if lane == "meta" and vals[4] <= 0:
                vals = vals[:4] + (_list_rate_cost(
                    vals[0], vals[1], vals[2], _META_RATES_PER_M),)
            elif lane == "opencode-go" and vals[4] <= 0:
                vals = vals[:4] + (_list_rate_cost(
                    vals[0], vals[1], vals[2], _OPENCODE_GO_RATES_PER_M),)
            w_by_day = weights.get(sid)
            if w_by_day:
                total_w = sum(w_by_day.values())
                for d, w in w_by_day.items():
                    if d not in day_index or total_w <= 0:
                        continue
                    i = day_index[d]
                    frac = w / total_w
                    series["input_tokens"][i] += vals[0] * frac
                    series["output_tokens"][i] += vals[1] * frac
                    series["cache_read_tokens"][i] += vals[2] * frac
                    series["calls"][i] += vals[3] * frac
                    series["cost_usd"][i] += vals[4] * frac
            else:
                d = _day(started) or _day(last)
                if d in day_index:
                    i = day_index[d]
                    series["input_tokens"][i] += vals[0]
                    series["output_tokens"][i] += vals[1]
                    series["cache_read_tokens"][i] += vals[2]
                    series["calls"][i] += vals[3]
                    series["cost_usd"][i] += vals[4]

            totals["input_tokens"] += vals[0]
            totals["output_tokens"] += vals[1]
            totals["cost_usd"] += vals[4]

        # Vectorizer retrieval calls per day — counted from tool-RESULT rows
        # (one result per completed call; assistant tool_calls blobs proved
        # unreliable for MCP calls).
        for d, n in cur.execute(
            "SELECT date(timestamp + ?, 'unixepoch') AS d, COUNT(*)"
            " FROM messages WHERE role='tool'"
            "   AND (tool_name LIKE '%vectorizer_ask%' OR tool_name LIKE '%vectorizer_search%')"
            " GROUP BY d",
            (_TZ_OFFSET,),
        ):
            if d:
                vec_calls[d] = vec_calls.get(d, 0.0) + float(n)

        # Measured result sizes: knowledge reads vs retrieval answers. Tool
        # rows have no token_count, so estimate from content length / 4.
        for tool_name, n, s in cur.execute(
            "SELECT tool_name, COUNT(*),"
            "       COALESCE(SUM(CASE WHEN COALESCE(token_count,0) > 0 THEN token_count"
            "               ELSE LENGTH(COALESCE(content,'')) / 4.0 END), 0)"
            " FROM messages WHERE role='tool' AND tool_name IS NOT NULL"
            " GROUP BY tool_name"
        ):
            entry = tool_sizes.setdefault(tool_name or "", [0.0, 0.0])
            entry[0] += float(n)
            entry[1] += float(s)
    except sqlite3.Error as exc:
        log.warning("costchart: query failed on %s: %s", db, exc)
    finally:
        con.close()


def _build_payload() -> Dict[str, Any]:
    # Day axis: a rolling WINDOW_DAYS window ending today (MYT), clipped to the
    # first day that actually has ledger activity.
    WINDOW_DAYS = 31
    today = _day(time.time())
    first: Optional[str] = None
    for db in _ledger_paths():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute(
                "SELECT MIN(started_at), MIN(last_activity_at) FROM sessions").fetchone()
            con.close()
        except sqlite3.Error:
            continue
        for v in row:
            d = _day(v)
            if d and (first is None or d < first):
                first = d
    window_start = _day(time.time() - (WINDOW_DAYS - 1) * 86400)
    start = first or today
    if window_start and start < window_start:
        start = window_start
    days: List[str] = []
    # TZ-independent anchors: timegm parses as UTC, so the axis covers
    # start..today (MYT dates) no matter the host's local timezone.
    # (time.mktime here read the date in host-local time and silently ended
    # the axis yesterday on UTC+8 hosts, dropping today's spend.)
    t = calendar.timegm(time.strptime(start, "%Y-%m-%d")) - _TZ_OFFSET
    end_t = calendar.timegm(time.strptime(today, "%Y-%m-%d")) - _TZ_OFFSET
    while t <= end_t + 1:
        days.append(_day(t))
        t += 86400
    if not days:
        days = [today]
    day_index = {d: i for i, d in enumerate(days)}

    buckets: Dict[str, Dict[str, List[float]]] = {}
    vec_calls: Dict[str, float] = {}
    tool_sizes: Dict[str, List[float]] = {}
    totals = {"input_tokens": 0.0, "output_tokens": 0.0, "cost_usd": 0.0}

    for db in _ledger_paths():
        _scan_db(db, buckets, day_index, vec_calls, tool_sizes, totals)

    for lane, series in buckets.items():
        for key in series:
            series[key] = [round(v, 6) for v in series[key]]

    # Savings model (documented, both inputs measured from the ledgers).
    def _avg(names) -> float:
        n = sum(tool_sizes.get(t, [0.0, 0.0])[0] for t in names)
        s = sum(tool_sizes.get(t, [0.0, 0.0])[1] for t in names)
        return (s / n) if n > 0 else 0.0

    avg_knowledge = round(_avg(_KNOWLEDGE_TOOLS), 1)
    retrieval_tools = [t for t in tool_sizes
                       if "vectorizer_ask" in t.lower() or "vectorizer_search" in t.lower()]
    avg_retrieval = round(_avg(retrieval_tools), 1)
    avoided_per_call = max(0.0, round(REPLACED_PER_LOOKUP * avg_knowledge - avg_retrieval, 1))
    blended_rate = (totals["cost_usd"] / (totals["input_tokens"] + totals["output_tokens"])
                    if (totals["input_tokens"] + totals["output_tokens"]) > 0 else 0.0)

    daily_calls = [vec_calls.get(d, 0.0) for d in days]
    daily_tokens_saved = [round(c * avoided_per_call, 1) for c in daily_calls]
    daily_usd_saved = [round(toks * blended_rate, 6) for toks in daily_tokens_saved]

    providers = {
        lane: {
            "daily": buckets.get(lane, _empty_series(days)),
            "total": {
                "cost_usd": round(sum(buckets.get(lane, _empty_series(days))["cost_usd"]), 4),
                "input_tokens": round(sum(buckets.get(lane, _empty_series(days))["input_tokens"])),
                "output_tokens": round(sum(buckets.get(lane, _empty_series(days))["output_tokens"])),
                "calls": round(sum(buckets.get(lane, _empty_series(days))["calls"])),
            },
        }
        for lane in ("xiaomi", "opencode-go", "meta", "local", "other")
    }

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + _TZ_OFFSET)),
        "timezone": "MYT (UTC+8)",
        "window_days": WINDOW_DAYS,
        "days": days,
        "providers": providers,
        "vectorizer": {
            "daily_calls": daily_calls,
            "daily_tokens_saved": daily_tokens_saved,
            "daily_usd_saved": daily_usd_saved,
            "total_calls": round(sum(daily_calls)),
            "total_tokens_saved": round(sum(daily_tokens_saved)),
            "total_usd_saved": round(sum(daily_usd_saved), 4),
            "method": {
                "avg_knowledge_read_tokens": avg_knowledge,
                "avg_vectorizer_result_tokens": avg_retrieval,
                "avoided_tokens_per_call": avoided_per_call,
                "blended_usd_per_token": blended_rate,
                "replaced_per_lookup": REPLACED_PER_LOOKUP,
                "meta_rates_per_m": dict(_META_RATES_PER_M),
                "meta_rates_source": ("muse-spark-1.3-contributor public list pricing "
                                      "(Baseer / explainx.ai / MyClaw.ai, Oct 2026)"),
                "opencode_go_rates_per_m": dict(_OPENCODE_GO_RATES_PER_M),
                "opencode_go_rates_source": ("same muse-spark-1.3-contributor list "
                                             "rates — ledger carries no zen price "
                                             "(cost_status unknown on 30/31 sessions "
                                             "checked 2026-10-06); the 1 priced session "
                                             "keeps its ledger estimate"),
                "formula": ("saved/day = vectorizer_calls/day × max(0, "
                            "replaced_per_lookup × avg_knowledge_read_tokens "
                            "− avg_vectorizer_result_tokens); "
                            "usd_saved = saved_tokens × blended_usd_per_token "
                            "(fleet est. cost ÷ non-cache tokens). "
                            "Floor estimate: excludes the search-explore tail "
                            "(e.g. session_search results up to ~26k tokens) and "
                            "the cache re-reads an avoided token skips on every "
                            "later turn of a long session."),
            },
        },
        "notes": [
            "Daily numbers are estimates: the ledger records totals per session, "
            "spread across the session's active days by message token weight (MYT).",
            "Costs are the ledger's estimated_cost_usd (fine for ranking spend, not invoicing).",
            "Meta is priced from public list rates (muse-spark-1.3-contributor: "
            "$0.10/M input, $0.20/M output, $0.002/M cached input; the ledger has "
            "no price for api.meta.ai) — computed per session from its tokens.",
            "OpenCode Go (opencode.ai/zen/go, muse-spark-1.3-contributor) uses the "
            "same list rates when the ledger carries no price (cost_status unknown "
            "— true for nearly all zen sessions checked); a ledger estimate wins "
            "whenever present and priced, so priced zen sessions keep their own cost.",
            "Local LM Studio runs cost $0 in API fees (compute/electricity only).",
            "Vectorizer savings are a documented estimate — the formula and its "
            "measured inputs ship in vectorizer.method.",
        ],
    }


@router.get("/daily")
def daily() -> Dict[str, Any]:
    global _cache, _cache_at
    with _cache_lock:
        if _cache and (time.monotonic() - _cache_at) < _CACHE_TTL:
            return _cache
        payload = _build_payload()
        _cache, _cache_at = payload, time.monotonic()
        return payload
