"""Phase 7: the optional AI analyst. Real SDK against a local fake API (no key, no network),
hard limits in the engine, the AI account vs its rules-only shadow, replay without re-billing,
no network inside the day's transaction, fallbacks, no look-ahead, no leaked key, and
backtests that never consult it."""

import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fake_anthropic import FakeAnthropic, error, message, verdict
from trader import timeutil
from trader.ai_report import ai_summary, verdict_text
from trader.backtest import make_engine
from trader.cli import app
from trader.config import AppConfig, LLMConfig, load_config
from trader.data import clean_and_validate
from trader.db import Database
from trader.llm import OUTPUT_SCHEMA, SYSTEM_PROMPT, Analyst, Review, Verdict, prompt_hash
from trader.runtime import Runtime
from trader.synthetic import generate

pytest.importorskip("anthropic")

T0 = datetime(2020, 11, 2, 0, 12, tzinfo=timezone.utc)  # first run processes 2020-11-01 (4 reviewable entries)
KEY = "sk-ant-test-CANARY-5f3a9d"
REPO = Path(__file__).resolve().parent.parent


def body_payload(body: dict) -> dict:
    return json.loads(body["messages"][0]["content"].split("\n\n", 1)[1])


def by_symbol(rules: dict):
    """Fake analyst: answer per coin, e.g. {"BTC": ("veto", 0.0)}; others approved."""
    def behave(body):
        sym = body_payload(body)["symbol"].split("/")[0]
        dec, mult = rules.get(sym, ("approve", 1.0))
        return 200, message(verdict(dec, mult, 0.7, [f"{sym}: {dec}"])), 0.0
    return behave


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnthropic()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f.url)
    yield f
    f.close()


def _ai_config(home, enabled=True, **llm):
    txt = home.config_file.read_text()
    txt = txt.replace("source: exchange", "source: synthetic")
    txt = re.sub(r"(?m)^  enabled: (true|false)\n  model:", f"  enabled: {'true' if enabled else 'false'}\n  model:", txt)
    txt = txt.replace("max_retries: 1", "max_retries: 0").replace("timeout_seconds: 120", "timeout_seconds: 5")
    for k, v in llm.items():
        txt = re.sub(rf"(?m)^  {k}: .*$", f"  {k}: {v}", txt)
    home.config_file.write_text(txt)


@pytest.fixture
def ai_home(home, fake):
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    _ai_config(home)
    home.env_file.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    timeutil.set_now(T0)
    return home


def rt_for(paths):
    return Runtime(load_config(paths), paths)


def at(days: int):
    timeutil.set_now(T0 + timedelta(days=days))


def q(paths, sql, *args):
    c = sqlite3.connect(paths.db_file)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def orders(paths, suffix):
    return q(paths, "SELECT o.created_bar_date, o.strategy, o.symbol, o.qty FROM orders o JOIN runs r ON r.id = o.run_id "
                    "WHERE r.run_key LIKE ? AND o.side = 'buy' AND o.reason = 'entry' ORDER BY 1, 2, 3", f"%:{suffix}")


# ------------------------------------------------------------------------------ verdict validation
def test_verdict_bounds_are_enforced_client_side():
    ok = Verdict(decision="reduce", size_multiplier=0.4, confidence=0.9, reasons=["  x  ", ""])
    assert ok.multiplier() == 0.4 and ok.reasons == ["x"]
    assert Verdict(decision="approve", size_multiplier=0.2, confidence=0.5, reasons=["a"]).multiplier() == 1.0
    assert Verdict(decision="veto", size_multiplier=0.9, confidence=0.5, reasons=["a"]).multiplier() == 0.0
    for bad in ({"size_multiplier": 1.5}, {"size_multiplier": -0.1}, {"confidence": 2}, {"reasons": []},
                {"decision": "buy_more"}, {"extra": 1}):
        with pytest.raises(Exception):
            Verdict(**{"decision": "reduce", "size_multiplier": 0.5, "confidence": 0.5, "reasons": ["a"], **bad})
    assert len(Verdict(decision="veto", size_multiplier=0, confidence=0, reasons=["x" * 5000]).reasons[0]) == 300


def test_prompt_hash_is_stable_and_covers_model_and_payload():
    p = {"b": 1.0, "a": [1, 2]}
    assert prompt_hash("claude-opus-5", p) == prompt_hash("claude-opus-5", {"a": [1, 2], "b": 1.0})
    assert prompt_hash("claude-opus-5", p) != prompt_hash("claude-sonnet-5", p)
    assert prompt_hash("claude-opus-5", p) != prompt_hash("claude-opus-5", {**p, "b": 1.1})


def test_config_pins_a_claude_model_and_validates_bounds():
    with pytest.raises(Exception):
        LLMConfig(model="gpt-4o")
    with pytest.raises(Exception):
        LLMConfig(timeout_seconds=0)
    assert LLMConfig(effort=None).effort is None
    assert AppConfig().llm.enabled is False  # off unless the user turns it on


# ------------------------------------------------------------------------------ the client (real SDK over HTTP)
def test_request_shape_pinned_model_schema_and_refusal_fallback(fake):
    a = Analyst(LLMConfig(timeout_seconds=5, max_retries=0), KEY)
    r = a.review({"symbol": "BTC/USD"})
    assert r.status == "ok" and r.decision == "approve" and r.multiplier == 1.0
    req = fake.requests[-1]
    b, h = req["body"], {k.lower(): v for k, v in req["headers"].items()}
    assert b["model"] == "claude-opus-5" and b["system"] == SYSTEM_PROMPT
    assert b["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert b["output_config"]["effort"] == "high" and "thinking" not in b and "temperature" not in b
    assert b["fallbacks"] == "default" and h["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert h["x-api-key"] == KEY and req["path"].startswith("/v1/messages")
    assert r.served_model == "claude-opus-5" and r.input_tokens == 3000 and r.output_tokens == 800
    assert r.cost_usd == pytest.approx((3000 * 5 + 800 * 25) / 1e6)
    assert r.prompt_hash == prompt_hash("claude-opus-5", {"symbol": "BTC/USD"})
    Analyst(LLMConfig(timeout_seconds=5, max_retries=0, refusal_fallback=False, effort=None), KEY).review({"x": 1})
    b2 = fake.requests[-1]["body"]
    assert "fallbacks" not in b2 and "effort" not in b2["output_config"] and "beta" not in fake.requests[-1]["path"]


@pytest.mark.parametrize("behaviour,reason", [
    (lambda b: (200, message("not json"), 0), "parse_error"),
    (lambda b: (200, message(verdict("reduce", 1.8)), 0), "parse_error"),  # out of range: never enlarges
    (lambda b: (200, message(json.dumps({"decision": "veto"})), 0), "parse_error"),
    (lambda b: (200, message("", stop="refusal", extra={"stop_details": {"type": "refusal", "category": "cyber",
                                                                          "explanation": "declined"}}), 0), "refusal"),
    (lambda b: (200, message('{"decision": "appr', stop="max_tokens"), 0), "truncated"),
    (lambda b: error(500, "api_error"), "api_error"),
    (lambda b: error(529, "overloaded_error"), "api_error"),
    (lambda b: error(429, "rate_limit_error"), "rate_limited"),
    (lambda b: error(401, "authentication_error"), "auth_error"),
    (lambda b: error(400, "invalid_request_error", "model not supported"), "bad_request"),
    (lambda b: (200, message(verdict()), 3), "timeout"),
])
def test_every_failure_becomes_a_rules_fallback(fake, behaviour, reason):
    fake.respond(behaviour)
    fast = LLMConfig(max_retries=0).model_copy(update={"timeout_seconds": 1})  # below the config minimum, for speed
    r = Analyst(fast, KEY).review({"symbol": "ETH/USD"})
    assert r.status == "fallback" and r.fallback_reason == reason, (r.fallback_reason, r.error)
    assert r.decision == "approve" and r.multiplier == 1.0 and r.is_failure  # = the rule-based decision


def test_no_key_network_down_and_missing_package_fall_back(monkeypatch):
    assert Analyst(LLMConfig(), None).review({}).fallback_reason == "no_key"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")  # nothing listens there
    fast = LLMConfig(max_retries=0).model_copy(update={"timeout_seconds": 2})
    assert Analyst(fast, KEY).review({}).fallback_reason == "network_error"
    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
                        if name == "anthropic" else real_import(name, *a, **k))
    assert Analyst(LLMConfig(), KEY).review({}).fallback_reason == "not_installed"


def test_a_client_that_raises_anything_never_breaks_the_caller():
    class Boom:
        class beta:  # noqa: N801
            class messages:  # noqa: N801
                @staticmethod
                def create(**kw):
                    raise ZeroDivisionError("weird")
    r = Analyst(LLMConfig(), KEY, client=Boom()).review({"a": 1})
    assert r.status == "fallback" and r.fallback_reason == "error"


# ------------------------------------------------------------------------------ engine hard limits
@pytest.fixture(scope="module")
def frames():
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    out = {}
    for a in ("BTC", "ETH", "SOL", "XRP"):
        df, rep = clean_and_validate(generate(a, end=date(2026, 9, 26)), f"{a}/USD", now=now)
        out[f"{a}/USD"] = df
    return out


class Stub:
    """An advisor that answers every entry with the same multiplier (possibly an illegal one)."""

    def __init__(self, mult, max_bars=60):
        self.mult, self.max_bars, self.requests = mult, max_bars, []

    def review(self, req):
        self.requests.append(req)
        return Review(decision="reduce", multiplier=self.mult, reasons=["stub"], status="ok", prompt_hash="h",
                      model="stub")


def _run(frames, advisor=None, end="2021-12-31"):
    syms = list(frames)
    eng, *_ = make_engine(AppConfig(), frames, ["S1", "S2", "S3"], symbols=syms, start="2021-01-01", end=end,
                          advisor=advisor)
    return eng.run(until=end)


@pytest.fixture(scope="module")
def rules_journal(frames):
    return _run(frames)


def _entry_orders(j):
    return [(o.created_date, o.strategy, o.symbol, round(o.qty, 12)) for o in j.orders if o.reason == "entry"]


@pytest.mark.parametrize("mult", [5.0, 1.0000001, math.inf, math.nan])
def test_the_ai_can_never_enlarge_a_trade(frames, rules_journal, mult):
    j = _run(frames, Stub(mult))
    assert _entry_orders(j) == _entry_orders(rules_journal)  # clamped to the risk engine's size (nan/inf -> rules)


def test_a_veto_cancels_and_a_reduction_shrinks_exactly(frames, rules_journal):
    vetoed = _run(frames, Stub(-3.0))  # illegal negative -> treated as a veto
    assert _entry_orders(vetoed) == [] and _entry_orders(rules_journal)
    j = _run(frames, Stub(0.5), end="2021-02-28")
    base = _run(frames, None, end="2021-02-28")
    first_ai, first_rules = _entry_orders(j)[0], _entry_orders(base)[0]
    assert first_ai[:3] == first_rules[:3] and first_ai[3] == pytest.approx(first_rules[3] * 0.5)
    assert any(e.type == "ai_reduce" for e in j.events)


def test_only_approved_entries_reach_the_ai_and_it_sees_no_future_data(frames, rules_journal):
    stub = Stub(1.0, max_bars=40)
    _run(frames, stub)
    kind = {sg["ref"]: sg["signal"] for sg in rules_journal.signals}
    approved = [d for d in rules_journal.decisions if kind[d["signal_ref"]] == "enter" and d["final_action"] == "buy"]
    adds = [d for d in rules_journal.decisions if kind[d["signal_ref"]] == "rebalance" and d["final_action"] == "buy"]
    assert len(stub.requests) == len(approved) > 5  # entries only: never exits, rebalance adds or blocked signals
    assert adds  # (S3 rebalance adds exist in this span, and were correctly not sent)
    for req in stub.requests:
        p = req.payload
        rows = p["daily_bars"]["rows"]
        assert p["decision_day"] == req.date and rows[-1][0] == req.date and len(rows) <= 40
        assert all(r[0] <= req.date for r in rows)
        assert p["proposal"]["side"] == "buy" and 0 < p["proposal"]["loss_if_stopped_pct_of_equity"] <= 0.0100001


def test_backtests_and_research_code_never_pass_an_advisor():
    offenders = []
    for f in (REPO / "trader").rglob("*.py"):
        if f.name in ("runtime.py", "backtest.py", "engine.py", "llm.py"):
            continue
        if re.search(r"advisor\s*=", f.read_text()):
            offenders.append(f.name)
    assert offenders == []
    src = (REPO / "trader/backtest.py").read_text()
    assert src.count("advisor=advisor") == 1  # make_engine only forwards what the live runtime gives it


def test_a_backtest_with_the_ai_enabled_makes_no_api_call(ai_home, fake):
    r = CliRunner().invoke(app, ["data", "fetch"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(app, ["backtest", "-s", "S1", "-s", "S3", "-a", "BTC", "-a", "XRP", "--no-save"])
    assert r.exit_code == 0, r.output
    assert fake.requests == []


# ------------------------------------------------------------------------------ live runtime
def test_ai_account_and_rules_only_shadow_start_together(ai_home, fake):
    fake.respond(by_symbol({"BTC": ("veto", 0.0), "XRP": ("reduce", 0.25)}))
    rt = rt_for(ai_home)
    assert [r.status for r in rt.catch_up("t")] == ["ok"]
    runs = dict((k, (m, s)) for k, m, s in q(ai_home, "SELECT run_key, mode, start FROM runs"))
    assert runs["paper:synthetic:AI"] == ("paper", "2020-11-01") and runs["paper:synthetic:AI_SHADOW"] == ("shadow", "2020-11-01")
    shadow, ai, portfolio = orders(ai_home, "AI_SHADOW"), orders(ai_home, "AI"), orders(ai_home, "PORTFOLIO")
    assert shadow == portfolio  # the twin is pure rules
    assert any(s == "BTC/USD" for _, _, s, _ in shadow) and not any(s == "BTC/USD" for _, _, s, _ in ai)
    sx = {(d, st): qty for d, st, s, qty in shadow if s == "XRP/USD"}
    ax = {(d, st): qty for d, st, s, qty in ai if s == "XRP/USD"}
    first = sorted(sx)[0]
    assert ax[first] == pytest.approx(sx[first] * 0.25)
    n_rev = q(ai_home, "SELECT COUNT(*) FROM ai_reviews")[0][0]
    assert n_rev == len(fake.requests) == 4
    llm = [json.loads(x) for (x,) in q(ai_home, "SELECT d.llm_json FROM decisions d JOIN signals s ON s.id = d.signal_id "
                                                "JOIN runs r ON r.id = s.run_id WHERE r.run_key LIKE '%:AI' AND d.llm_json IS NOT NULL")]
    assert len(llm) == 4 and all(x["model"] == "claude-opus-5" and len(x["prompt_hash"]) == 64 for x in llm)
    assert q(ai_home, "SELECT COUNT(*) FROM decisions d JOIN signals s ON s.id = d.signal_id JOIN runs r ON r.id = s.run_id "
                      "WHERE r.run_key LIKE '%:AI_SHADOW' AND d.llm_json IS NOT NULL")[0][0] == 0
    types = [t for (t,) in q(ai_home, "SELECT type FROM risk_events WHERE type LIKE 'ai_%'")]
    assert types.count("ai_veto") == 1 and types.count("ai_reduce") >= 1 and "ai_fallback" not in types


def test_stored_reviews_capture_what_was_asked_and_contain_no_future(ai_home, fake):
    rt = rt_for(ai_home)
    for k in range(3):
        at(k)
        rt.catch_up("t")
    rows = q(ai_home, "SELECT bar_date, request_json, model, served_model, prompt_version, cost_usd, status FROM ai_reviews")
    assert rows
    for d, req, model, served, pv, cost, status in rows:
        p = json.loads(req)
        assert p["decision_day"] == d and max(r[0] for r in p["daily_bars"]["rows"]) == d
        assert p["data_note"].startswith("SYNTHETIC") and model == served == "claude-opus-5" and pv == "1"
        assert status == "ok" and cost > 0


def test_a_rerun_day_replays_stored_verdicts_and_never_pays_twice(ai_home, fake, monkeypatch):
    import trader.runtime as runtime_mod

    rt = rt_for(ai_home)
    rt.catch_up("t")
    calls_day1 = len(fake.requests)
    assert rt.prepare_ai_reviews("2020-11-01", rt.md.load_all(), rt.md.symbols()) == 0  # already stored
    at(1)
    real = runtime_mod.write_journal
    boom = {"n": 0}

    def fail_once(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("disk hiccup")
        return real(*a, **k)

    monkeypatch.setattr(runtime_mod, "write_journal", fail_once)
    assert rt.catch_up("t")[0].status == "failed"
    after_fail = len(fake.requests)
    rt._failed_at.clear()
    assert rt.catch_up("t")[0].status == "ok"
    assert len(fake.requests) == after_fail  # same verdicts replayed, not re-bought
    assert after_fail >= calls_day1
    assert q(ai_home, "SELECT COUNT(*) FROM risk_events WHERE type = 'ai_fallback'")[0][0] == 0


def test_no_api_call_ever_happens_inside_the_days_transaction(ai_home, fake, monkeypatch):
    depth = {"n": 0, "calls": 0}
    real_tx = Database.tx

    @contextmanager
    def tracked(self):
        depth["n"] += 1
        try:
            with real_tx(self) as c:
                yield c
        finally:
            depth["n"] -= 1

    real_review = Analyst.review

    def guarded(self, payload):
        assert depth["n"] == 0, "AI call inside a database transaction"
        depth["calls"] += 1
        return real_review(self, payload)

    monkeypatch.setattr(Database, "tx", tracked)
    monkeypatch.setattr(Analyst, "review", guarded)
    rt = rt_for(ai_home)
    for k in range(3):
        at(k)
        assert all(r.status == "ok" for r in rt.catch_up("t"))
    assert depth["calls"] >= 4


def test_api_down_means_the_rules_decide_and_it_is_visible(ai_home, fake):
    fake.respond(lambda b: error(500, "api_error"))
    rt = rt_for(ai_home)
    assert rt.catch_up("t")[0].status == "ok"  # an AI outage never blocks trading
    assert orders(ai_home, "AI") == orders(ai_home, "AI_SHADOW")
    fb = q(ai_home, "SELECT status, fallback_reason FROM ai_reviews")
    assert fb and all(r == ("fallback", "api_error") for r in fb)
    assert q(ai_home, "SELECT COUNT(*) FROM risk_events WHERE type = 'ai_fallback' AND severity = 'warn'")[0][0] == len(fb)


def test_switching_it_off_keeps_the_pair_running_without_calls(ai_home, fake):
    rt = rt_for(ai_home)
    rt.catch_up("t")
    n = len(fake.requests)
    _ai_config(ai_home, enabled=False)
    rt = rt_for(ai_home)
    for k in (1, 2, 3):
        at(k)
        rt.catch_up("t")
    assert len(fake.requests) == n
    days = q(ai_home, "SELECT COUNT(DISTINCT e.bar_date) FROM equity_snapshots e JOIN runs r ON r.id = e.run_id "
                      "WHERE r.run_key LIKE '%:AI'")[0][0]
    assert days == 4  # no gaps in the comparison
    later = [json.loads(x) for (x,) in q(ai_home, "SELECT d.llm_json FROM decisions d JOIN signals s ON s.id = d.signal_id "
                                                  "WHERE s.bar_date > '2020-11-01' AND d.llm_json IS NOT NULL")]
    assert all(x["fallback_reason"] == "disabled" for x in later)
    assert q(ai_home, "SELECT COUNT(*) FROM risk_events WHERE type = 'ai_fallback'")[0][0] == 0  # a setting, not a failure


def test_old_catch_up_days_are_decided_by_the_rules(ai_home, fake):
    rt = rt_for(ai_home)
    rt.catch_up("t")
    at(40)  # the Mac was off for 40 days; max_age_days = 7
    assert all(r.status == "ok" for r in rt.catch_up("t"))
    asked = {d for (d,) in q(ai_home, "SELECT DISTINCT bar_date FROM ai_reviews")}
    last_closed = (T0 + timedelta(days=40)).date() - timedelta(days=1)
    cutoff = (last_closed - timedelta(days=7)).isoformat()
    assert asked and all(d == "2020-11-01" or d >= cutoff for d in asked)
    old = [json.loads(x)["fallback_reason"] for d, x in q(
        ai_home, "SELECT s.bar_date, d.llm_json FROM decisions d JOIN signals s ON s.id = d.signal_id "
                 "WHERE d.llm_json IS NOT NULL AND s.bar_date > '2020-11-01' AND s.bar_date < ?", cutoff)]
    assert old and set(old) == {"too_old"}


def test_stop_while_waiting_for_the_ai_defers_the_day_cleanly(ai_home, fake):
    rt = rt_for(ai_home)

    def stop_then_answer(body):
        rt.stop_requested.set()  # `trader stop` arrives while the first review is in flight
        return 200, message(verdict()), 0.0

    fake.respond(stop_then_answer)
    res = rt.catch_up("t")
    assert res[0].status == "deferred"
    assert q(ai_home, "SELECT status, error FROM job_runs")[0][0] == "failed"
    assert q(ai_home, "SELECT COUNT(*) FROM equity_snapshots")[0][0] == 0  # nothing traded or written
    assert q(ai_home, "SELECT COUNT(*) FROM ai_reviews")[0][0] == 1  # the answer that arrived is kept
    fake.respond(lambda b: (200, message(verdict()), 0.0))
    rt2 = rt_for(ai_home)
    assert rt2.catch_up("t")[0].status == "ok"
    assert q(ai_home, "SELECT COUNT(*) FROM ai_reviews")[0][0] == 4


def test_the_api_key_never_reaches_logs_or_the_database(ai_home, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.respond(lambda b: error(401, "authentication_error", "invalid x-api-key"))
    rt = rt_for(ai_home)
    rt.catch_up("t")
    fake.respond(lambda b: (200, message(verdict()), 0.0))
    at(1)
    rt.catch_up("t")
    assert KEY not in caplog.text
    for f in ai_home.logs.glob("*"):
        assert KEY not in f.read_text(errors="replace"), f
    c = sqlite3.connect(ai_home.db_file)
    assert KEY not in "\n".join(c.iterdump())


# ------------------------------------------------------------------------------ measurement + surfaces
def test_verdict_text_is_honest_about_small_samples():
    assert verdict_text(10, 3.0, None)[0] == "info" and "Too early" in verdict_text(10, 3.0, None)[1]
    lvl, txt = verdict_text(200, 0.8, 6.2)
    assert lvl == "info" and "No evidence" in txt and "6 more year" in txt
    assert verdict_text(400, 2.5, 0)[0] == "ok" and verdict_text(400, -2.4, 0)[0] == "bad"


def test_report_and_dashboard_card_match_the_database(ai_home, fake):
    from fastapi.testclient import TestClient

    from trader.server import create_app

    fake.respond(by_symbol({"BTC": ("veto", 0.0), "XRP": ("reduce", 0.5)}))
    rt = rt_for(ai_home)
    for k in range(5):
        at(k)
        rt.catch_up("t")
    s = ai_summary(rt.db, "synthetic")
    eq = dict(q(ai_home, "SELECT r.run_key, e.equity FROM equity_snapshots e JOIN runs r ON r.id = e.run_id "
                         "WHERE e.bar_date = '2020-11-05'"))
    assert s["difference"] == pytest.approx(eq["paper:synthetic:AI"] - eq["paper:synthetic:AI_SHADOW"])
    assert s["calls"] == len(fake.requests) and s["verdicts"].get("veto", 0) >= 1
    assert s["cost_usd"] == pytest.approx(q(ai_home, "SELECT SUM(cost_usd) FROM ai_reviews")[0][0])
    assert s["level"] == "info" and "Too early" in s["verdict"]
    rt.heartbeat()
    with TestClient(create_app(load_config(ai_home), ai_home, rt, start_scheduler=False), client=("127.0.0.1", 9)) as cl:
        html = cl.get("/?acct=AI").text
        assert 'data-testid="ai-card"' in html and "Too early to judge" in html
        m = re.search(r'data-testid="ai-difference" data-value="([^"]+)"', html)
        assert float(m.group(1)) == pytest.approx(s["difference"])
        assert ">Shadow</a>" in html and ">AI</a>" in html
        assert "AI analyst: not used" in cl.get("/?acct=AI_SHADOW").text or "No closed trades" in cl.get("/?acct=AI_SHADOW").text
    r = CliRunner().invoke(app, ["llm", "report"])
    assert r.exit_code == 0 and "rules-only shadow twin" in r.output and "Too early" in r.output


def test_no_ai_card_or_accounts_until_it_is_switched_on(home):
    from fastapi.testclient import TestClient

    from trader.server import create_app

    CliRunner().invoke(app, ["init"])
    _ai_config(home, enabled=False)
    timeutil.set_now(T0)
    rt = rt_for(home)
    rt.catch_up("t")
    assert [k for (k,) in q(home, "SELECT run_key FROM runs") if ":AI" in k] == []
    rt.heartbeat()
    with TestClient(create_app(load_config(home), home, rt, start_scheduler=False), client=("127.0.0.1", 9)) as cl:
        assert 'data-testid="ai-card"' not in cl.get("/").text
    r = CliRunner().invoke(app, ["llm", "report"])
    assert r.exit_code == 0 and "has not run yet" in r.output


def test_llm_test_command(ai_home, fake):
    assert CliRunner().invoke(app, ["data", "fetch"]).exit_code == 0
    fake.respond(lambda b: (200, message(verdict("reduce", 0.5, 0.6, ["Stretched 3 ATR."]), tin=4000, tout=1000), 0))
    r = CliRunner().invoke(app, ["llm", "test"])
    assert r.exit_code == 0 and "✅ reduce" in r.output and "Stretched 3 ATR." in r.output and "$0.0450" in r.output
    p = body_payload(fake.requests[-1]["body"])
    assert "CONNECTION TEST" in p["note"] and len(p["daily_bars"]["rows"]) == 60
    fake.respond(lambda b: error(401, "authentication_error"))
    r = CliRunner().invoke(app, ["llm", "test"])
    assert r.exit_code == 1 and "auth_error" in r.output and KEY not in r.output


def test_doctor_reports_the_ai_setup(home, monkeypatch):
    from trader.doctor import check_ai
    from trader.config import Secrets

    cfg = AppConfig()
    assert check_ai(cfg, Secrets()).status == "skip"
    on = AppConfig(llm=LLMConfig(enabled=True))
    assert check_ai(on, Secrets()).status == "fail" and "ANTHROPIC_API_KEY" in check_ai(on, Secrets()).detail
    ok = check_ai(on, Secrets(anthropic_api_key=KEY))
    assert ok.status == "ok" and KEY not in ok.detail and "llm test" in ok.detail
