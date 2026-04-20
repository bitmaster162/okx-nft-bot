## Известные проблемы (обновлено 2026-04-20 после Phase 1–6 + AUDIT-V2 fixes)

### ✅ FIXED
- **offer_blaster.py:269** — ETH Seaport v1.5 → v1.6 (commit 7f7dd7e)
- **K4**: offer_blaster.py:559 — EIP-712 domain version "1.5" → "1.6" (commit 763baf9)
- **K5**: verified via live OKX probe (commit 03cfd37). `orderType=2` (FULL_RESTRICTED)
  is correct for the single-unit atomic offers produced by `_build_offer_payload`.
  The WIP 2→3 change was discarded intentionally.
- **A1**: stub `ExecutionGovernor` renamed to `ExecutionGovernorV2Stub` (commit f73b167),
  then entire v2-experiment cluster deleted as D1–D5 (commit f3df899).
- **A2**: stub `PnLEngine` marked deprecated (commit c455dc7), then deleted as D2 (f3df899).
- **A4**: `DRY_RUN=""` now treated as default-True (commit f5680de).
- **B1**: parasite_hunter dynamic qty = `min(max_qty, budget_usd/price)` (commit c5a0ca1).
- **K1**: `_find_all_our_offers` — local tracker merged always, not only on empty API
  (commit e2c1442). Partial/foreign API results no longer hide our live offers.
- **K3**: `_PLACE_COOLDOWN` now persists to SQLite via `PositionState.place_cooldown`
  table (commits 0717b55 + 1c1925b). Parasite hunter hydrates
  `_last_placed`/`_placed_qty_cache` on init and upserts after every placement —
  container restarts no longer reset the anti-stacking guard.
- **A9**: BSC-only chain checks centralized into `config.validate_execution_chain`
  + `SUPPORTED_EXECUTION_CHAINS` (commit 08133d5). `execution_governor`,
  `counterbid/engine` (×2), `undercutter/engine` (×2) and `execution_cli._ensure_bsc`
  now delegate to the helper. Hardcoded-BNB sites (`mass_offer/engine`,
  `signing/seaport_signer.preview_counterbid`) keep the explicit check with a
  `# BSC-only:` comment documenting the constraint.
- **K7**: Current master still reads `sell_settings.min_listing_price_bnb` and
  clamps `our_sell_price` in both sell branches (`parasite_hunter.py:3146`,
  :3315, :3329). The safety floor was never removed; the WIP stash entry
  that would have dropped it is discarded. No code change required.
- **K6**: `_buyer_lock` verified present at parasite_hunter.py:239/1006/1046. The WIP
  removal lived only in `stash@{0}` and was never applied — no code change needed.
- **K8/K9**: execution_daemon now has one `[RECONCILE]` block per cycle (not two) and
  iterates `RECONCILE_CHAINS` env (default "bsc,eth") instead of hardcoded bsc
  (commit e2c1442).
- **A5**: undercutter skips state write when `submit_offer` returns empty `offer_id`
  — no more orphan "dryrun-"-prefixed hash for live orders (commit e2c1442).
- **A6**: undercutter DEFENSE is now submit-then-retire. Old offer is only cancelled
  after the new one lands on the exchange (commit e2c1442).
- **A7**: `detect_spreads` groups by currency before min/max — no more garbage
  BNB↔ETH "spreads" (commit e2c1442).
- **D1–D5**: 10 dead files (~560 KB) removed (commit f3df899). Active code paths
  unaffected.
- **Q4**: r/s split WIP verified not applied — `counterbid/okx_api.py` still sends
  `r=""`, `s=""`, full signature. No rollback needed.
- **BUG-1**: ETH `last_reconcile_chain` timestamp wiped by `audit_integrity`
  because the hardcoded `!= "bsc"` predated A9. Replaced with
  `SUPPORTED_EXECUTION_CHAINS` check in `undercutter/state.py` (commit aa393d2).
- **BUG-2**: `SQLiteStore`, `OffersStore`, `FraudStore` opened connections
  without WAL → concurrent writers saw `database is locked`. All three now
  set `PRAGMA journal_mode=WAL`, `busy_timeout=10000` on every connect and use
  `timeout=10.0` on `sqlite3.connect` (commit 62650d2).
- **RISK-1**: Per-process nonce cache in `buyer.py` + unguarded
  `get_transaction_count` in `counterbid/okx_api.py` risked nonce collisions
  between containers. Centralized in `ExecutionGovernor.allocate_nonce` with a
  SQLite `wallet_nonce` table and BEGIN IMMEDIATE atomic allocation (commit
  215ba2d). Mirrors the existing `allocate_seaport_counter` pattern.
- **RISK-2**: Auto-resolved by RISK-1 — the `wallet_nonce` SQLite row is the
  persistent source of truth, so container restart recovers state. No separate
  commit.
- **RISK-3**: Single-RPC paths in 4 sites (`signing/seaport_signer.get_counter`,
  `execution_governor`, `counterbid/okx_api`, `sniper/parasite_hunter`)
  meant one RPC outage stopped execution. Added `config.get_rpc_urls(chain)`
  (reads `<CHAIN>_RPC_URLS` CSV + legacy `BUYER_RPC_URL*`), per-URL retry loop
  with jittered sleep in `get_counter`, `allocate_nonce` failover (commit 18f2c5e).
- **RISK-4**: `parasite_hunter._get_balance` called `urllib.request.urlopen`
  directly → bypassed rate limiter → 429s on public RPCs. Added shared
  `get_rpc_transport()` in `clients/http.py` (StdlibHttpTransport backed by a
  shared `RateLimiter`) and routed `_get_balance` through it (commit 9d9c301).
- **RISK-6** (mitigation): Multi-container shared-key overshoot of OKX rate.
  Added `CONTAINER_COUNT_FOR_RATE_SPLIT` env (default 1); effective
  `okx_rate_limit_per_sec = OKX_RATE_LIMIT_PER_SEC / CONTAINER_COUNT_FOR_RATE_SPLIT`.
  Operators running N containers should set the env to N and set
  `OKX_RATE_LIMIT_PER_SEC` to the total budget (commit 1d726b8). Full fix
  (IPC-based global limiter) deferred.
- **SMELL-1**: `"0x" + signed.signature.hex()` duplicated in 4 sites.
  Added `hex_with_prefix(bytes)` helper in `signing/seaport_signer.py`;
  replaced at `counterbid/okx_api.py`, `sniper/offer_blaster.py`,
  `clients/opensea.py`, and the helper site itself (commit 209d1f8).
- **DEBT**: stray debug scripts and backup dumps relocated out of `config/`
  (commit 8850291): `gen_eth_config_auto.py`, `parse_and_merge_wallet.py`,
  `parse_wallet_offers.py` → `scripts/debug/`;
  `buy_config.json.bak`, `wallet_missing_collections.json` → `data/backups/`.
  `config/okx_nft_bot_v13_hardened/` left frozen as historical snapshot.

### ⚠️ PARTIAL

**K2: Ghost-branch proceed при неполной он-чейн отмене**
- parasite_hunter.py:2539-2542 (ghost-branch now at lines ~2570-2610)
- `_cancel_onchain_seaport` уже ждёт receipt (status==1) перед возвратом True,
  поэтому on-chain ветка фактически верифицирована. API-only ветка
  (`cancel-listing` endpoint) отдаёт 200 без tx — для неё on-chain проверки нет.
- Частичный фикс (commit bdca0e4): в ghost-branch логирование разделено —
  INFO "receipt verified" когда `_last_onchain_cancel_ts < 30s`, WARNING
  "cancel was API-only, no on-chain verification" иначе. Поведение proceed
  оставлено (альтернатива — infinite ABORT loop при залипшем API-кеше).
- Полный фикс невозможен без возврата tx hash из API-only пути; статус PARTIAL.

### 🔴 CONFIRMED, NOT FIXED

_(none from AUDIT-V2 — all P0/P1 items landed in Phase 6)_

### Historical (resolved in earlier phases)

**A3: Stale Seaport counter при множественных движках на одном кошельке**
- Resolved by central `ExecutionGovernor.allocate_seaport_counter` (commit
  19c4bcb) — SQLite `seaport_counter` table with BEGIN IMMEDIATE atomic
  allocation, shared between all engines on the same (wallet, chain).

**A8: `UNDERCUTTER_MIN_PRICE_BNB` — safety floor только для undercutter**
- Resolved by `ExecutionGovernor.check_min_price` (commit 5a8c788) — single
  floor now applied across undercutter, offer_blaster, mass_offer, and
  parasite_hunter strategy branches.

### 🟠 OPEN QUESTIONS

**Q1: Rate limit 429 от OKX на 40% запросов**
- Даже при `OKX_RATE_LIMIT_PER_SEC=0.3` продолжается 429 (50011)
- Гипотеза: 3 контейнера × свой лимитер на процесс = overshoot
- Решение: либо IPC-lock между контейнерами, либо второй сервер с отдельным IP

**Q2: CR7-коллекция — были офферы выше floor?**
- Kimi наставил дублей, потом юзер вручную отменил
- Нужна ретроспектива в логах: были ли submit > floor_price
- Сейчас USDT=0 на кошельке, новых офферов нет

**Q3: wash-trading детектор**
- `0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7` ↔
  `0x660bfc61854a8cc6fa0bd03b1331fd85709af7dc` гоняют NFT между собой
- Оба попали в PARASITE_WALLETS — видимо ошибочно для этих
- Нужна логика: если две pair-wallet из списка часто матчатся между собой
  на одной коллекции → флаг wash, не конкурировать там

---

## Стэш `stash@{0}` ("WIP Kimi/Cowork — K5-K9 Q4 under review")

Из него НИЧЕГО не применено в основной ветке. Содержимое разобрано и:
- **K5** — отвергнуто (verified via live API, commit 03cfd37).
- **K6** — отвергнуто (лок нужен, текущая ветка его уже содержит).
- **K7** — отвергнуто (master сохраняет `min_listing_price_bnb` safety-floor).
- **K8** — фикс-идея верная, но форма стэша добавляла ДВА reconcile (проблема).
  Применена правильная форма (один reconcile, commit e2c1442).
- **K9** — та же история: исправлено multi-chain вариантом в e2c1442.
- **Q4** — отвергнуто (неправильный r/s split, без переноса `v`).

Стэш можно `git stash drop 'stash@{0}'` после финального review. На момент
записи оставлен как исторический артефакт.
