"""Static guarantees: no code path can place orders or use exchange credentials."""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "trader"

FORBIDDEN = [
    r"\bcreate_order\b", r"\bcreateOrder\b", r"\bcreate_market_\w*order\b", r"\bcreate_limit_\w*order\b",
    r"\bcancel_order\b", r"\bedit_order\b", r"\bwithdraw\b", r"\btransfer\b", r"\bfetch_balance\b",
    r"\bprivate(Get|Post|Put|Delete)\w*", r"\bset_leverage\b",
    r"apiKey\s*[:=]", r"['\"]secret['\"]\s*:",
]


def test_no_order_or_private_endpoint_code():
    offenders = []
    for py in PKG.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            for m in re.finditer(pat, src):
                offenders.append(f"{py.relative_to(PKG.parent)}: {m.group(0)}")
    assert not offenders, "order/credential code found:\n" + "\n".join(offenders)


def test_env_example_has_no_exchange_keys():
    env = (PKG.parent / ".env.example").read_text()
    assert not re.search(r"(BINANCE|BITSTAMP|COINBASE|KRAKEN|EXCHANGE)_?(API)?_?(KEY|SECRET)", env, re.I)


def test_gitignore_covers_secrets_and_state():
    gi = (PKG.parent / ".gitignore").read_text().split()
    for entry in (".env", "data/", "logs/", "run/", ".venv/"):
        assert entry in gi
