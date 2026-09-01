"""An optional language model, kept at arm's length from the numbers.

The app's promise is that every figure is reproducible from rows you can read.
A model cannot make that promise, so it is not allowed anywhere near a figure.
It gets exactly two jobs, both at the edges:

  1. **Propose rules** for merchants no rule matched. It never writes a
     category onto a transaction. It proposes a rule, you accept it, and the
     accepted rule is an ordinary user rule -- visible on /rules, editable,
     deletable, and applied by the same deterministic matcher as everything
     else. Run the same import twice and you get the same answer, with or
     without a model configured.

  2. **Narrate** findings that were already computed. It is handed finished
     sentences and asked to join them up. It is never asked what something
     costs, and never sees a number it could restate wrongly, because every
     number in its input is already a string.

Cost, in the order the savings matter:

  * Deduplicated. Two hundred uncategorised transactions are usually twenty
    distinct merchants. The model sees the twenty.
  * Cached forever, keyed on the merchant. A merchant is sent at most once in
    the lifetime of the database, whether you accept the suggestion or not.
  * Batched. One request for the whole backlog, not one per merchant.
  * Capped. A hard limit per run, a short max_tokens, temperature 0.
  * Manual. Nothing here runs on import, on a page load, or on a timer. It
    runs when you press the button.

Free by default: the reference backend is Ollama on localhost, which costs
nothing and sends nothing off the machine. Any OpenAI-compatible endpoint
works instead -- several have free tiers -- by setting FINASST_LLM_URL. No
new dependency: this speaks HTTP with the standard library.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import db
from .categorize import merchant_key, normalise
from .config import CATEGORIES

DEFAULT_URL = "http://127.0.0.1:11434/v1/chat/completions"   # Ollama, local and free
DEFAULT_MODEL = "llama3.2:3b"
MAX_MERCHANTS_PER_RUN = 40
TIMEOUT_SECONDS = 90


@dataclass
class Config:
    url: str
    model: str
    api_key: str = ""
    enabled: bool = False

    @property
    def is_local(self) -> bool:
        host = re.sub(r"^https?://", "", self.url).split("/")[0].split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

    @property
    def where(self) -> str:
        return "on this machine" if self.is_local else self.url


def config(conn: Optional[sqlite3.Connection] = None) -> Config:
    """Environment first, then the settings table, then off."""
    url = os.environ.get("FINASST_LLM_URL", "").strip()
    model = os.environ.get("FINASST_LLM_MODEL", "").strip()
    key = os.environ.get("FINASST_LLM_KEY", "").strip()
    if conn is not None:
        url = url or db.get_setting(conn, "llm_url", "").strip()
        model = model or db.get_setting(conn, "llm_model", "").strip()
    enabled = bool(url) or (conn is not None and db.get_setting(conn, "llm_enabled", "") == "1")
    return Config(url=url or DEFAULT_URL, model=model or DEFAULT_MODEL,
                  api_key=key, enabled=enabled)


class LLMError(RuntimeError):
    """Anything that went wrong, phrased for someone who is not debugging it."""


def _call(cfg: Config, messages: list[dict], max_tokens: int) -> str:
    payload = json.dumps({
        "model": cfg.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    request = urllib.request.Request(cfg.url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LLMError(
            f"Could not reach the model at {cfg.url}. "
            + ("Is Ollama running? `ollama serve`, then `ollama pull "
               f"{cfg.model}`." if cfg.is_local else str(exc.reason))
        ) from exc
    except (ValueError, KeyError) as exc:
        raise LLMError(f"The model replied with something that was not JSON: {exc}") from exc
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Unexpected reply shape from the model: {body}") from exc


# ---------------------------------------------------------------------------
# Rule proposals
# ---------------------------------------------------------------------------

@dataclass
class Suggestion:
    merchant_key: str
    example: str
    category: Optional[str]
    confidence: str
    model: str
    status: str = "proposed"
    transactions: int = 0


def pending_merchants(conn: sqlite3.Connection, limit: int = MAX_MERCHANTS_PER_RUN) -> list[dict]:
    """Distinct merchants with no category and no cached suggestion.

    This is the deduplication that does most of the cost saving: the model sees
    one row per merchant, not one per transaction.
    """
    rows = conn.execute(
        "SELECT description, COUNT(*) n, SUM(-amount) total FROM transactions "
        "WHERE category IS NULL GROUP BY lower(description) ORDER BY COUNT(*) DESC"
    ).fetchall()
    seen = {r["merchant_key"] for r in conn.execute("SELECT merchant_key FROM llm_suggestions")}
    out = []
    for r in rows:
        key = merchant_key(r["description"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "example": r["description"], "count": int(r["n"]),
                    "total": round(float(r["total"] or 0), 2)})
        if len(out) >= limit:
            break
    return out


PROMPT = (
    "You label bank-statement merchant strings with a spending category. "
    "Reply with JSON only: an array of objects with keys \"merchant\", "
    "\"category\", \"confidence\" (high|medium|low). Use ONLY these categories: "
    "{categories}. If a merchant does not clearly belong to any of them, set "
    "category to null rather than guessing. These are Canadian statements."
)


def suggest_categories(conn: sqlite3.Connection, limit: int = MAX_MERCHANTS_PER_RUN) -> list[Suggestion]:
    """One batched request for the whole uncategorised backlog."""
    cfg = config(conn)
    if not cfg.enabled:
        raise LLMError("No model is configured. Turn one on below first.")

    pending = pending_merchants(conn, limit)
    if not pending:
        return []

    listing = "\n".join(f"- {p['example']}" for p in pending)
    content = _call(cfg, [
        {"role": "system", "content": PROMPT.format(categories=", ".join(CATEGORIES))},
        {"role": "user", "content": listing},
    ], max_tokens=40 * len(pending) + 200)

    parsed = _parse_labels(content)
    by_key = {normalise(k): v for k, v in parsed.items()}

    made = []
    for p in pending:
        label = by_key.get(normalise(p["example"])) or by_key.get(p["key"]) or {}
        category = label.get("category")
        if category not in CATEGORIES:
            category = None
        confidence = str(label.get("confidence") or "low").lower()
        conn.execute(
            "INSERT INTO llm_suggestions(merchant_key, example, category, confidence, model) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(merchant_key) DO UPDATE SET "
            "example = excluded.example, category = excluded.category, "
            "confidence = excluded.confidence, model = excluded.model",
            (p["key"], p["example"], category, confidence, cfg.model),
        )
        made.append(Suggestion(p["key"], p["example"], category, confidence, cfg.model,
                               transactions=p["count"]))
    conn.commit()
    return made


def _parse_labels(content: str) -> dict:
    """Pull the JSON array out of a reply that may be wrapped in prose or fences."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return {}
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("merchant"):
            out[str(item["merchant"])] = item
    return out


def suggestions(conn: sqlite3.Connection, status: str = "proposed") -> list[Suggestion]:
    rows = conn.execute(
        "SELECT * FROM llm_suggestions WHERE status = ? ORDER BY confidence, merchant_key",
        (status,),
    ).fetchall()
    counts = {
        normalise(r["description"]): int(r["n"])
        for r in conn.execute(
            "SELECT description, COUNT(*) n FROM transactions WHERE category IS NULL "
            "GROUP BY lower(description)")
    }
    out = []
    for r in rows:
        hits = sum(n for desc, n in counts.items() if r["merchant_key"] in desc)
        out.append(Suggestion(r["merchant_key"], r["example"], r["category"],
                              r["confidence"], r["model"], r["status"], hits))
    return out


def accept(conn: sqlite3.Connection, merchant_key_value: str, category: Optional[str] = None) -> str:
    """Turn a suggestion into an ordinary user rule.

    After this the model is out of the loop entirely: the rule is on /rules
    like any other, and the categorisation is done by the same matcher that
    handles everything else.
    """
    from . import rules

    row = conn.execute(
        "SELECT * FROM llm_suggestions WHERE merchant_key = ?", (merchant_key_value,)
    ).fetchone()
    if row is None:
        raise LLMError("No such suggestion.")
    chosen = category or row["category"]
    if not chosen:
        raise LLMError("That suggestion has no category to accept. Pick one yourself.")
    rules.add_rule(conn, row["merchant_key"], chosen, match_type="words")
    conn.execute("UPDATE llm_suggestions SET status = 'accepted', category = ? "
                 "WHERE merchant_key = ?", (chosen, merchant_key_value))
    conn.commit()
    return chosen


def reject(conn: sqlite3.Connection, merchant_key_value: str) -> None:
    """Say no, and remember saying no, so it is never sent again."""
    conn.execute("UPDATE llm_suggestions SET status = 'rejected' WHERE merchant_key = ?",
                 (merchant_key_value,))
    conn.commit()


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------

NARRATE_PROMPT = (
    "You are summarising findings a personal-finance tool has already computed. "
    "Write at most four short sentences of plain English for the person whose "
    "money this is. Rules: use ONLY the figures given to you, never invent or "
    "recompute one, never add advice about products or investments, and do not "
    "flatter or reassure. If the findings are minor, say they are minor."
)


def narrate(conn: sqlite3.Connection, findings: list, forecast_basis: str = "") -> str:
    """Join already-computed findings into a paragraph.

    Every number in the input is already formatted as text, so the model has
    nothing to calculate and nothing to get wrong.
    """
    cfg = config(conn)
    if not cfg.enabled:
        raise LLMError("No model is configured. Turn one on below first.")
    if not findings:
        return "Nothing worth reporting this month."

    lines = "\n".join(f"- {f.headline}. {f.detail}" for f in findings[:8])
    if forecast_basis:
        lines += f"\n- Forecast basis: {forecast_basis}"
    return _call(cfg, [
        {"role": "system", "content": NARRATE_PROMPT},
        {"role": "user", "content": lines},
    ], max_tokens=260).strip()


def status(conn: sqlite3.Connection) -> dict:
    cfg = config(conn)
    counts = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM llm_suggestions GROUP BY status")
    }
    return {
        "enabled": cfg.enabled,
        "url": cfg.url,
        "model": cfg.model,
        "is_local": cfg.is_local,
        "where": cfg.where,
        "proposed": counts.get("proposed", 0),
        "accepted": counts.get("accepted", 0),
        "rejected": counts.get("rejected", 0),
        "cached": sum(counts.values()),
        "pending": len(pending_merchants(conn, MAX_MERCHANTS_PER_RUN)),
    }
