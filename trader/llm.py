"""Optional AI analyst layer: live paper trading only, never in backtests.

What it may do: look at ONE entry the risk engine has already approved and answer approve,
reduce (a size multiplier between 0 and 1) or veto. What it may not do: create a trade,
enlarge one, move a stop, or touch an exit. Those limits are enforced in code (the engine
clamps the multiplier to 0..1 and only asks about entries), not by asking in the prompt.

Every review is recorded with the requested model, the model that actually answered, a
hash of the exact prompt, tokens, cost and latency. Any problem (no key, package missing,
timeout, API error, refusal, output that is not valid in-range JSON) falls back to the
rule-based decision, and the reason is recorded.

Why never in backtests: a language model may have memorised historical prices, so a
backtest with it in the loop would be meaningless. Its value is measured going forward
instead, against a rules-only shadow account that sees exactly the same signals.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trader.config import LLMConfig

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_REASON_CHARS = 300

# USD per million tokens (input, output), for the cost shown on the dashboard. A model not
# listed here still works; its cost is simply shown as unknown.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
}

SYSTEM_PROMPT = """\
You are a skeptical risk analyst reviewing one proposed trade in a paper-trading simulation \
(no real money). A rules-based, long-only, daily trend-following strategy on spot crypto \
generated the entry signal, and a deterministic risk engine has already approved its size \
and stop-loss.

You can only keep the trade as it is ("approve"), make it smaller ("reduce", with a \
size_multiplier strictly between 0 and 1), or cancel it ("veto", size_multiplier 0). You \
cannot enlarge it, change its stop, or affect exits; the software enforces this.

Base your judgement only on the data provided: daily candles up to and including the close \
of the decision day, the strategy's indicator values, the proposed size and stop, the open \
positions and the risk state. The order would fill at the next day's open. Do not use any \
knowledge of prices after the decision day.

Keep the base rates in mind. Trend-following entries typically win only 30-45% of the time; \
the profits come from a few large winners, so vetoing or shrinking too often destroys the \
edge the rules are designed to capture. Approve unless you see a concrete problem in the \
data, for example: a move already very extended relative to its average true range, a \
breakout on a bar that is an outlier in range or volume, heavy overlap with coins already \
held, or an unusually large loss if the stop is hit. When you reduce or veto, say which \
numbers led you there.

Reply with: decision; size_multiplier (1.0 for approve, 0.0 for veto); confidence (0 to 1, \
how sure you are that your decision beats simply following the rules); and 1-4 short \
plain-English reasons that cite the data."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reduce", "veto"]},
        "size_multiplier": {"type": "number", "description": "1.0 for approve, 0.0 for veto, between 0 and 1 for reduce"},
        "confidence": {"type": "number", "description": "0 to 1"},
        "reasons": {"type": "array", "items": {"type": "string"}, "description": "1-4 short reasons citing the data"},
    },
    "required": ["decision", "size_multiplier", "confidence", "reasons"],
    "additionalProperties": False,
}


class Verdict(BaseModel):
    """The analyst's answer, validated client-side (the API's JSON-schema mode cannot enforce
    numeric ranges). Anything outside these bounds is a parse failure -> rules decision."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reduce", "veto"]
    size_multiplier: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(min_length=1, max_length=6)

    @field_validator("reasons")
    @classmethod
    def _tidy(cls, v: list[str]) -> list[str]:
        out = [r.strip()[:MAX_REASON_CHARS] for r in v if r and r.strip()]
        if not out:
            raise ValueError("no reasons given")
        return out

    def multiplier(self) -> float:
        """Effective size multiplier: approve keeps the size, veto cancels, reduce shrinks."""
        if self.decision == "approve":
            return 1.0
        if self.decision == "veto":
            return 0.0
        return self.size_multiplier


@dataclass
class Review:
    """One review of one proposed entry. `status` is "ok" when the analyst answered and
    "fallback" when the rule-based decision stands (see `fallback_reason`)."""

    decision: str
    multiplier: float
    reasons: list[str]
    status: str
    prompt_hash: str
    model: str
    confidence: float | None = None
    fallback_reason: str | None = None  # no_key | not_installed | timeout | network_error | auth_error |
    # rate_limited | bad_request | api_error | refusal | truncated | parse_error | error | disabled | too_old | not_reviewed
    served_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    response_text: str | None = None
    error: str | None = None
    prompt_version: str = PROMPT_VERSION

    @classmethod
    def fallback(cls, reason: str, prompt_hash: str, model: str, error: str | None = None, **kw) -> "Review":
        why = {"disabled": "AI analyst is switched off", "too_old": "day too old for an AI review",
               "not_reviewed": "no stored AI review for this exact proposal"}.get(reason, f"AI review failed ({reason})")
        return cls(decision="approve", multiplier=1.0, reasons=[f"{why}; the rule-based decision stands."],
                   status="fallback", prompt_hash=prompt_hash, model=model, fallback_reason=reason,
                   error=error[:500] if error else None, **kw)

    @property
    def is_failure(self) -> bool:
        """A fallback caused by something going wrong (not by a deliberate setting)."""
        return self.status == "fallback" and self.fallback_reason not in ("disabled", "too_old")

    def to_json(self) -> dict:
        """Compact form stored with the decision (decisions.llm_json) and shown on the dashboard."""
        keys = ("decision", "multiplier", "confidence", "reasons", "status", "fallback_reason", "model",
                "served_model", "prompt_hash", "prompt_version", "cost_usd", "latency_ms")
        d = asdict(self)
        out = {k: d[k] for k in keys}
        out["size_multiplier"] = self.multiplier  # the name the dashboard / spec use
        return out


# ------------------------------------------------------------------------------ the request
STRATEGY_RULES = {
    "S1": "Donchian breakout: buy when the close exceeds the highest high of the previous entry_lookback days; "
          "exit on a close below the lowest low of the previous exit_lookback days, or below a chandelier stop "
          "(highest high since entry minus chandelier_mult x ATR).",
    "S2": "Supertrend: buy when the Supertrend direction flips up while the close is above its trend_sma-day "
          "average; exit when it flips down.",
    "S3": "Time-series momentum: hold while both the short_lookback- and long_lookback-day returns are positive "
          "and the close is above its trend_sma-day average; each coin is sized to a share of a volatility target.",
}

@dataclass
class ReviewRequest:
    """Everything the analyst sees about one proposed entry, as of the close of `date`."""

    date: str
    symbol: str
    strategy: str
    run_key: str = ""
    payload: dict = field(default_factory=dict)


def _r(x, sig: int = 6):
    """Round for a stable prompt (and prompt hash) across platforms."""
    if x is None or isinstance(x, (bool, str)):
        return x
    x = float(x)
    if not math.isfinite(x):
        return None
    return float(f"{x:.{sig}g}")


def clean(obj):
    """JSON-safe copy with floats rounded (numpy scalars included)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _r(obj)
    return obj


def user_message(payload: dict) -> str:
    return ("Review this proposed trade. All data is as of the close of the decision day (UTC).\n\n"
            + json.dumps(payload, sort_keys=True, separators=(",", ":")))


def prompt_hash(model: str, payload: dict) -> str:
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, model, SYSTEM_PROMPT, json.dumps(OUTPUT_SCHEMA, sort_keys=True), user_message(payload)):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


def cost_usd(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = PRICES.get(model or "")
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


# ------------------------------------------------------------------------------ the client
class Analyst:
    """Calls the model. `review()` never raises: every failure becomes a rules fallback."""

    def __init__(self, cfg: LLMConfig, api_key: str | None, *, client=None):
        self.cfg = cfg
        self._api_key = api_key
        self._client = client

    def _get_client(self):
        if self._client is None:
            import anthropic  # optional dependency (requirements-llm.txt)

            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=self.cfg.timeout_seconds,
                                               max_retries=self.cfg.max_retries)
        return self._client

    def review(self, payload: dict) -> Review:
        model = self.cfg.model
        ph = prompt_hash(model, payload)
        if not self._api_key and self._client is None:
            return Review.fallback("no_key", ph, model, "ANTHROPIC_API_KEY is not set in .env")
        try:
            import anthropic
        except ImportError:
            if self._client is None:
                return Review.fallback("not_installed", ph, model,
                                       "the anthropic package is not installed (pip install -r requirements-llm.txt)")
            anthropic = None
        params = {
            "model": model, "max_tokens": self.cfg.max_tokens, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message(payload)}],
            "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        }
        if self.cfg.effort:
            params["output_config"]["effort"] = self.cfg.effort
        t0 = time.monotonic()
        try:
            client = self._get_client()
            if self.cfg.refusal_fallback:
                resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
            else:
                resp = client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 - every failure must become a rules fallback
            reason = _classify(exc, anthropic)
            log.warning("AI review failed (%s): %s", reason, _short(exc))
            return Review.fallback(reason, ph, model, _short(exc), latency_ms=int((time.monotonic() - t0) * 1000))
        latency = int((time.monotonic() - t0) * 1000)
        served = getattr(resp, "model", None)
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "input_tokens", None)
        tout = getattr(usage, "output_tokens", None)
        meta = {"served_model": served, "input_tokens": tin, "output_tokens": tout, "latency_ms": latency,
                "cost_usd": cost_usd(served or model, tin, tout)}
        text = next((b.text for b in (resp.content or []) if getattr(b, "type", None) == "text"), None)
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            det = getattr(resp, "stop_details", None)
            why = f"declined (category: {getattr(det, 'category', None)})"
            log.warning("AI review declined by the model: %s", why)
            return Review.fallback("refusal", ph, model, why, response_text=text, **meta)
        if stop == "max_tokens":
            return Review.fallback("truncated", ph, model, "answer cut off at max_tokens", response_text=text, **meta)
        try:
            v = Verdict.model_validate(json.loads(text or ""))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            log.warning("AI review output unusable: %s", _short(exc))
            return Review.fallback("parse_error", ph, model, _short(exc), response_text=(text or "")[:2000], **meta)
        return Review(decision=v.decision, multiplier=v.multiplier(), reasons=v.reasons, status="ok", prompt_hash=ph,
                      model=model, confidence=v.confidence, response_text=text[:2000], **meta)


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _classify(exc: Exception, anthropic) -> str:
    if anthropic is None:
        return "error"
    table = [
        ("APITimeoutError", "timeout"), ("AuthenticationError", "auth_error"), ("PermissionDeniedError", "auth_error"),
        ("RateLimitError", "rate_limited"), ("BadRequestError", "bad_request"), ("NotFoundError", "bad_request"),
        ("APIStatusError", "api_error"), ("APIConnectionError", "network_error"),
    ]
    for name, reason in table:  # most specific first (APITimeoutError is an APIConnectionError)
        cls = getattr(anthropic, name, None)
        if cls is not None and isinstance(exc, cls):
            return reason
    return "error"


# ------------------------------------------------------------------------------ inside the engine
class ReviewBook:
    """The engine's advisor. Pure lookup, no network: reviews are fetched BEFORE the day's
    database transaction (runtime.prepare_ai_reviews) and replayed here, so re-running a day
    gives exactly the same decisions and never pays for the same review twice.

    collect=True (dry run): the FIRST proposal of the day with no stored review is noted in
    `missing` and provisionally approved, so the caller can fetch it and run again. Only the
    first, because every later proposal that day depends on its answer (a veto frees cash, risk
    budget and a position slot), so asking about them now could pay for reviews never used."""

    def __init__(self, run_key: str, stored: dict[str, Review], *, model: str, max_bars: int,
                 synthetic: bool, collect: bool = False, policy: str | None = None):
        self.run_key = run_key
        self.stored = stored
        self.model = model
        self.max_bars = max_bars
        self.synthetic = synthetic
        self.collect = collect
        self.policy = policy  # "disabled" / "too_old": decide by the rules without asking
        self.missing: dict[str, ReviewRequest] = {}

    def review(self, req: ReviewRequest) -> Review:
        req.run_key = self.run_key
        if self.synthetic:
            req.payload["data_note"] = "SYNTHETIC test prices, not a real market"
        ph = prompt_hash(self.model, req.payload)
        if ph in self.stored:
            return self.stored[ph]
        if self.policy:
            return Review.fallback(self.policy, ph, self.model)
        if self.collect and not self.missing:
            self.missing[ph] = req
        return Review.fallback("not_reviewed", ph, self.model)
