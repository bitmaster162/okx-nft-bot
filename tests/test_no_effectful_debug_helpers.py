from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_ROOT = REPO_ROOT / "scripts" / "debug"
REMOVED_DIRECT_WRITE_HELPERS = {
    DEBUG_ROOT / "seaport_cancel_all.py",
}
DIRECT_CHAIN_WRITE_PRIMITIVES = (
    "send_raw_transaction",
)


def test_removed_direct_write_helpers_stay_absent() -> None:
    present = sorted(str(path.relative_to(REPO_ROOT)) for path in REMOVED_DIRECT_WRITE_HELPERS if path.exists())
    assert present == [], f"direct-write helper(s) reintroduced: {present}"


def test_debug_scripts_do_not_submit_raw_chain_transactions() -> None:
    offenders: list[str] = []
    if DEBUG_ROOT.exists():
        for path in sorted(DEBUG_ROOT.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if any(primitive in text for primitive in DIRECT_CHAIN_WRITE_PRIMITIVES):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], (
        "debug scripts must not bypass execution governance with direct raw-chain writes: "
        f"{offenders}"
    )
