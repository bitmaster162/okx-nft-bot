# Codex Task: Fix CounterBid/Undercutter State & Killswitch Bugs

## Context

The Parasite Killer NFT bot v17 has two execution engines that submit offers to OKX:

- **CounterBidder** (`src/okx_nft_bot/counterbid/engine.py`) — detects parasite wallet offers and places counter-bids above them
- **UndercutEngine** (`src/okx_nft_bot/undercutter/engine.py`) — monitors our active offers, defends against competitors, attacks weak collections

Both engines share a single **PositionState** (`src/okx_nft_bot/undercutter/state.py`) backed by SQLite for tracking live offers in the `active_offers` table. The `/killswitch` Telegram command (`src/okx_nft_bot/telegram_bot.py`) iterates `active_offers` to cancel everything and force dry-run mode.

An audit found **5 bugs** where live offers can escape tracking and survive killswitch. Fix all 5.

---

## BUG 1 (CRITICAL): CounterBidder does NOT track live offers in `active_offers`

### File: `src/okx_nft_bot/counterbid/engine.py`

### Problem

In `process_single_collection()`, when a **LIVE_COUNTERBID** is successfully submitted (lines 237-253), the code:
1. ✅ Calls `self.okx.submit_offer(preview_payload)` — offer goes live on OKX
2. ✅ Calls `self.state.record_submit_event(...)` — logged in `execution_submit_log`
3. ✅ Calls `self._send_live_notification(...)` — Telegram alert sent
4. ❌ **Never calls `self.state.upsert_active_offer()`** — offer is NOT tracked

Compare with `UndercutEngine._apply_action()` which correctly calls `self.state.upsert_active_offer()` at lines 221 (dry) and 260 (live).

### Consequences

- `/killswitch` cannot see or cancel counterbid offers
- Dashboard does not show them
- Rate limiting by active offer count ignores counterbid offers
- These offers live on OKX uncontrolled forever

### Fix

After the successful `submit_offer()` call (line 237) and after setting `action_type = "LIVE_COUNTERBID"`, add:

```python
offer_id = str(submit_result.get('offer_id', 'unknown'))
self.state.upsert_active_offer(
    order_hash=offer_id,
    collection=cfg.address,
    chain=resolved_chain,
    price_bnb=counter_price_bnb,
    status="active",
    current_floor=parasite_price_bnb if parasite_price_bnb > 0 else None,
    preview_payload=preview_payload,
)
```

Also do the same for the **DRY_COUNTERBID** path (when `effective_dry_run` is True, lines 207-208). Generate a synthetic order_hash like undercutter does:

```python
import hashlib
seed = f"{cfg.address.lower()}:{counter_price_bnb:.18f}:COUNTERBID:{preview_payload or {}}"
dry_order_hash = "dryrun-cb-" + hashlib.sha256(seed.encode()).hexdigest()[:24]
self.state.upsert_active_offer(
    order_hash=dry_order_hash,
    collection=cfg.address,
    chain=resolved_chain,
    price_bnb=counter_price_bnb,
    status="active",
    current_floor=parasite_price_bnb if parasite_price_bnb > 0 else None,
    preview_payload=preview_payload,
)
```

### Tests

Add to `tests/test_counter_bidder.py`:
- Test that after a successful `process_single_collection()` with `sign_preview=True` and `dry_run=False`, `state.get_active_offers()` returns the new offer
- Test that after a DRY_COUNTERBID, `state.get_active_offers()` also returns it (with `dryrun-cb-` prefix)
- Test that `/killswitch` cancels counterbid offers too (mock `cancel_offer`)

---

## BUG 2 (SERIOUS): DEFENSE action does not cancel old offer on OKX

### File: `src/okx_nft_bot/undercutter/engine.py`

### Problem

In `_apply_action()`, when `action.action_type == "DEFENSE"` (lines 211-212):

```python
if action.action_type == "DEFENSE" and action.order_hash:
    self.state.mark_offer_status(order_hash=action.order_hash, status="outbid")
```

This marks the old offer as "outbid" **locally only**. It never calls `self.offer_client.cancel_offer(action.order_hash)` to cancel it on OKX. The new (higher) offer is submitted, but the old one remains live on OKX. Over time, orphan offers accumulate.

### Fix

Before marking as outbid, cancel the old offer on OKX (in live mode only):

```python
if action.action_type == "DEFENSE" and action.order_hash:
    effective_dry_run = self.state.effective_dry_run(self.settings.dry_run)
    if not effective_dry_run and not action.order_hash.startswith("dryrun-"):
        try:
            self.offer_client.cancel_offer(action.order_hash)
            logger.info("Cancelled old offer %s before defense re-bid", action.order_hash)
        except Exception as exc:
            logger.warning("Failed to cancel old offer %s during defense: %s", action.order_hash, exc)
            # Continue anyway — the new offer will still be placed
    self.state.mark_offer_status(order_hash=action.order_hash, status="outbid")
```

### Tests

Add to `tests/test_undercut_engine.py`:
- Test that DEFENSE action calls `cancel_offer()` on the old order_hash before submitting the new one (live mode)
- Test that in dry-run mode, `cancel_offer()` is NOT called for DEFENSE

---

## BUG 3 (SERIOUS): Killswitch leaves zombie offers on failed cancel

### File: `src/okx_nft_bot/telegram_bot.py`

### Problem

In `_killswitch_command()` (lines 396-406): if `api.cancel_offer()` raises an exception or returns `False`, the offer stays `status="active"` in the DB. But killswitch immediately sets `force_dry_run=True`. Result: the daemon sees these "active" offers but operates in dry-run mode, so it can never cancel them on OKX. They become permanent zombies.

### Fix

After the loop, mark any remaining failed offers with a special status so they don't pollute `get_active_offers()`:

```python
# After the for-loop, before set_force_dry_run:
for fail_entry in failed:
    order_hash = fail_entry.split(":")[0]
    state.mark_offer_status(
        order_hash=order_hash,
        status="killswitch_failed",
    )
```

Also update `get_active_offers()` in `state.py` — it already filters `WHERE status = 'active'`, so `"killswitch_failed"` offers will be excluded automatically. But for visibility, add a separate query method:

```python
def get_killswitch_failed_offers(self, *, chain: str | None = None) -> list[ActiveOffer]:
    clauses = ["status = 'killswitch_failed'"]
    params: list[Any] = []
    if chain:
        clauses.append("chain = ?")
        params.append(chain.lower())
    where = " AND ".join(clauses)
    with self._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT order_hash, collection, chain, price_bnb, status, placed_at, last_checked_at, current_floor, preview_payload_json
            FROM active_offers
            WHERE {where}
            ORDER BY placed_at ASC
            """,
            params,
        ).fetchall()
    return [self._row_to_offer(row) for row in rows]
```

Update the killswitch response to include these:
```
f'zombies={len(failed)} (marked killswitch_failed, need manual cancel)'
```

### Tests

Add to `tests/test_counter_bidder.py` or new `tests/test_killswitch.py`:
- Test that when `cancel_offer` raises Exception, the offer is marked `"killswitch_failed"` (not left as `"active"`)
- Test that `get_active_offers()` does NOT return `"killswitch_failed"` offers
- Test `get_killswitch_failed_offers()` returns them

---

## BUG 4 (MEDIUM): undercut-daemon defaults to `--cycles 1`, not infinite

### File: `src/okx_nft_bot/execution_cli.py`

### Problem

Line 180:
```python
undercut_daemon.add_argument("--cycles", type=int, default=1)
```

The systemd service (`deploy/systemd/okx-nft-exec.service`) runs:
```
ExecStart=...okx-nft-exec undercut-daemon --interval 30
```

Without `--cycles`, it defaults to 1. The daemon runs one cycle and exits. systemd restarts it after 15 seconds (`RestartSec=15`), adding unnecessary overhead and gaps.

### Fix (two changes)

**A) In `execution_cli.py`:** Change default to 0, meaning "infinite":

```python
undercut_daemon.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 = infinite)")
```

**B) In `undercutter/scheduler.py`:** Handle `cycles=0` as infinite loop:

```python
def run_daemon(
    self,
    *,
    cycles: int,
    interval_seconds: int | None = None,
    chain: str | None = None,
    refresh: bool = False,
) -> DaemonResult:
    resolved_interval = interval_seconds or self.settings.undercut_interval_seconds
    runs: list[list[dict[str, Any]]] = []
    index = 0
    while True:
        actions = self.engine.run_cycle(chain=chain, refresh=refresh)
        runs.append([asdict(action) for action in actions])
        index += 1
        if cycles > 0 and index >= cycles:
            break
        if resolved_interval > 0:
            time.sleep(resolved_interval)
    return DaemonResult(cycles=index, interval_seconds=resolved_interval, runs=runs)
```

Note: `DaemonResult.runs` will grow in memory forever for infinite mode. Consider capping it (keep only last N runs) or removing it for infinite mode.

### Tests

- Test `run_daemon(cycles=3)` still works and returns exactly 3 runs
- Test `run_daemon(cycles=0)` runs indefinitely (use a mock that raises StopIteration after 5 cycles to break the loop in test)

---

## BUG 5 (MEDIUM): Stale dry-run offers accumulate in `active_offers` forever

### File: `src/okx_nft_bot/undercutter/state.py`

### Problem

Every dry-run ATTACK/DEFENSE creates a new row in `active_offers` with a unique `dryrun-<hash>` order_hash. Old offers get marked `"outbid"` or `"cancelled"` but are never deleted. Over weeks/months the table grows unboundedly.

### Fix

Add a cleanup method to `PositionState`:

```python
def cleanup_stale_offers(self, *, max_age_days: int = 7) -> int:
    """Delete non-active offers older than max_age_days."""
    with self._connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM active_offers
            WHERE status != 'active'
              AND placed_at < datetime('now', ? || ' days')
            """,
            (f"-{max_age_days}",),
        )
    return cursor.rowcount
```

Call it from `UndercutScheduler.run_daemon()` once per ~100 cycles (or once per hour):

```python
# Inside the main loop, after run_cycle:
if index % 100 == 0:
    cleaned = engine.state.cleanup_stale_offers(max_age_days=7)
    if cleaned:
        logger.info("Cleaned up %d stale offers from active_offers", cleaned)
```

### Tests

- Test that `cleanup_stale_offers()` deletes `"cancelled"` and `"outbid"` offers older than 7 days
- Test that it does NOT delete `"active"` offers
- Test that it does NOT delete recent `"cancelled"` offers (< 7 days)

---

## Files to modify

1. `src/okx_nft_bot/counterbid/engine.py` — BUG 1 (add `upsert_active_offer` for live + dry counterbids)
2. `src/okx_nft_bot/undercutter/engine.py` — BUG 2 (cancel old offer on OKX before DEFENSE re-bid)
3. `src/okx_nft_bot/telegram_bot.py` — BUG 3 (mark failed offers as `killswitch_failed`)
4. `src/okx_nft_bot/undercutter/state.py` — BUG 3 + BUG 5 (add `get_killswitch_failed_offers()` + `cleanup_stale_offers()`)
5. `src/okx_nft_bot/execution_cli.py` — BUG 4 (change `--cycles` default to 0)
6. `src/okx_nft_bot/undercutter/scheduler.py` — BUG 4 + BUG 5 (infinite loop + periodic cleanup)

## Test files to update/create

1. `tests/test_counter_bidder.py` — BUG 1 tests
2. `tests/test_undercut_engine.py` — BUG 2 tests
3. `tests/test_killswitch.py` (NEW) — BUG 3 tests
4. `tests/test_undercut_state.py` — BUG 5 tests
5. `tests/test_undercut_scheduler.py` (may need creation) — BUG 4 tests

## Validation

After all fixes, run:
```bash
python -m pytest tests/ -v --tb=short
python -m py_compile src/okx_nft_bot/counterbid/engine.py
python -m py_compile src/okx_nft_bot/undercutter/engine.py
python -m py_compile src/okx_nft_bot/undercutter/state.py
python -m py_compile src/okx_nft_bot/undercutter/scheduler.py
python -m py_compile src/okx_nft_bot/telegram_bot.py
python -m py_compile src/okx_nft_bot/execution_cli.py
```

All tests must pass and all files must compile without errors.
