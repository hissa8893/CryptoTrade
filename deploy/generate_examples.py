"""Regenerate the example files in deploy/ from the same code `trader service install` uses.
Run: .venv/bin/python deploy/generate_examples.py   (a test fails if they drift)."""

from pathlib import Path

from trader.paths import Paths
from trader.service import launchd_plists, make_ctx, systemd_units, windows_task_xml

HERE = Path(__file__).resolve().parent
EXAMPLE_ROOT = "/Users/you/CryptoTrade"


def render() -> dict[str, bytes]:
    ctx = make_ctx(Paths(Path(EXAMPLE_ROOT)), home=Path("/Users/you"), python=f"{EXAMPLE_ROOT}/.venv/bin/python")
    out = {f"launchd/{k}": v for k, v in launchd_plists(ctx).items()}
    out |= {f"systemd/{k}": v.replace("/Users/you", "/home/you").encode() for k, v in systemd_units(ctx).items()}
    wctx = make_ctx(Paths(Path(r"C:\Users\you\CryptoTrade")), python=r"C:\Users\you\CryptoTrade\.venv\Scripts\pythonw.exe")
    # UTF-16 as the XML declares (what Task Scheduler's "Import Task..." and schtasks /XML expect)
    out["windows/trader-task.xml"] = windows_task_xml(wctx).encode("utf-16")
    out["windows/heartbeat-task.xml"] = windows_task_xml(wctx, heartbeat=True).encode("utf-16")
    return out


if __name__ == "__main__":
    for rel, data in render().items():
        (HERE / rel).write_bytes(data)
        print("wrote", rel)
