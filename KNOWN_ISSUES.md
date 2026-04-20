## Известные проблемы (обновлено 2026-04-20 после Phase 1–4 + K2/K3/A9/K7)

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

**A3: Stale Seaport counter при множественных движках на одном кошельке**
- `mass_offer/engine.py:218` — counter читается один раз в начале `run()`,
  далее инкрементируется локально.
- Если параллельно работают 2+ движка на одном private_key, или юзер
  руками делает `incrementCounter()` во время прогона — все последующие
  офферы батча получат stale counter и будут отвергнуты контрактом.
- Частичный фикс (commit e2c1442): добавлен предупреждающий комментарий в
  коде. Архитектурный фикс (per-offer counter read или SQLite-локальный
  атомарный счётчик) — отдельной задачей.
- P1 (архитектурный)

**A8: `UNDERCUTTER_MIN_PRICE_BNB` — safety floor только для undercutter**
- `undercutter/engine.py:207` — env-driven минимум 0.0001 BNB применяется
  только в `UndercutEngine._apply_action`.
- `offer_blaster.py`, `mass_offer/engine.py`, `parasite_hunter.py` (strategy section) —
  нет аналогичного guard'а.
- Фикс: единая min-price-floor в `ExecutionGovernor.check_live_submit_allowed`.
- P2

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
