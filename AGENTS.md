# AGENTS.md — OKX NFT Bot ("Parasite-Killer")

**Project:** OKX Web3 (BSC) NFT counter-bidder / sniper
**Risk profile:** **A3** (financial, on-chain, irreversible offers & buys)
**BRAIN policy:** GPT-S:CORE v6.3.3 · AGENT §28 Solo-Operator MVCS
**Status:** `Implemented, unverified` — critical-control change 2026-07-19 (dashboard lockdown); A3 revalidation pending (см. §Tests).
**Owner / sole operator / approver:** Robert
**Git baseline at last change:** HEAD `2540905` (branch master; working tree dirty (84) — reversibility via per-file `.bak.<ts>` backups, not a clean commit).

---

## PURPOSE
Ставит контр-биды и снайпит листинги на OKX (BSC), продаёт свой инвентарь, охотится на кошельки-«паразиты». Автономный высокочастотный цикл. Реальные деньги, реальные on-chain действия → A3.

## ARCHITECTURE (physical)
- `okx-nft-bot-exec` (Docker) — исполнитель; код `src/okx_nft_bot/sniper/counter_bidder.py` (~4400 loc) + `parasite_hunter.py`, `buyer.py`.
- `execution_governor.py` — **единый pre-execution gate** (`check_live_submit_allowed`).
- `undercutter/state.py` — control-plane state в `data/execution.sqlite3` (arm/kill/nonce/counter/audit-таблицы).
- Каналы управления: **Telegram** `/armlive /disarmlive /killswitch` (аутентифицирован Телеграмом), дашборд (теперь **только localhost:8090**), SSH.

## CORE FLOW (execution path)
`scan rivals/listings → decide (price/undercut) → engine.place_single_offer → governor.check_live_submit_allowed → sign (Seaport 1.6) → submit (OKX API / on-chain) → record submit_log → reconcile`

## GATES — governor.check_live_submit_allowed (порядок)
1. `effective_dry_run` (3 слоя: configured OR DRY_RUN OR force_dry_run) → блок
2. `check_min_price` (MIN_OFFER_PRICE_USD)
3. `get_killswitch_failed_offers` → зомби-офферы блокируют ВСЕ новые сабмиты до ручной отмены
4. `is_force_dry_run` (kill-switch) → блок
5. `arm_state.armed` (+ expiry) → нужен живой arm
6. rate limit `MAX_LIVE_OFFERS_PER_HOUR`
7. **daily spend cap** `MAX_BNB_PER_DAY` (BNB-эквивалент)
8. `SUBMIT_COOLDOWN_SECONDS`

## BUDGETS (bounded effect)
`MAX_BNB_PER_DAY=5.0` (~$2.8k) · `MAX_LIVE_OFFERS_PER_HOUR=30` · `SUBMIT_COOLDOWN_SECONDS=10` · per-offer `COUNTERBID_MAX_USD` (BSC) / `_MAX_USD_ETH` · пулы `BUDGET_WL/NONWL/BUY_USD`. Превышение → сабмит блокируется, пишется `status="blocked"`.

## ACTION TAXONOMY
On-chain offer / buy / cancel = **IRREVERSIBLE** (отправка ≠ успех; последующее исправление не считается rollback).

## FAIL POLICY
Fail-**closed**: невалидный `force_dry_run` → integrity-quarantine форсит dry-run; ошибка чтения state → dry-run; зомби-офферы → блок всех сабмитов. По умолчанию бот в проигрыше склоняется к «не действовать».

## KILL-SWITCH
`force_dry_run` в `execution_runtime_state`. Проверяется в gate ПЕРЕД каждым сабмитом; независим от генеративного пути (это не LLM-решение, а детерминированный флаг в БД). Каналы: TG `/killswitch`, `/disarmlive`; state-слой. **Fail-closed.**

## ALWAYS / ASK / NEVER
- ALWAYS: сабмит только через governor gate; dry-run по умолчанию; писать submit_log; печать секретов запрещена.
- ASK (оператор): арм на живой режим (`/armlive N`); изменение лимитов/kill-switch/approval → ревалидация.
- NEVER: сабмит в обход gate; `force`-обход; коммит секретов; печать PRIVATE_KEY.

## ACCESS / TRUST BOUNDARIES
- Дашборд: **127.0.0.1:8090** (закрыт из интернета 2026-07-19; доступ SSH-туннелем). Управление также через TG (аутентиф.).
- `PRIVATE_KEY` / `BUYER_WALLET_PRIVATE_KEY` — только в `data`/`.env` на сервере 35, `.env` в `.gitignore`. Не выносить.
- SSH root — единственный полный доступ; помощник `tormoz` — полный sudo (⚠️ слабый пароль, см. accepted-risk R4).

## AUDIT (tamper-evident)
Таблицы `execution_submit_log` / `execution_fill_log` / `undercut_log` + `action_lifecycle` + **`audit_sealer.py`** (systemd `audit-sealer.timer`, 15 мин) — hash-chain seal в `/root/audit_chain.jsonl`, ловит правку/удаление записей и снимает control-plane identity (arm/kill + digest governor/state/counter_bidder/config + control-env). ExecStartPost гоняет `action_lifecycle.py --confirm-sweep`. Проверка: `python3 /root/audit_sealer.py --verify`.

## STATE MACHINE (#3)
`action_lifecycle.py` — журнал переходов действия (отдельная БД `data/action_lifecycle.sqlite3`, uid1000/0666). Хук в `state.record_submit_event` (единственный choke point всех submit'ов, additive + try/except, торговлю не трогает). Состояния: PROPOSED→VALIDATED→SUBMITTED / →REJECTED / →FAILED, и CONFIRMED из confirm-sweep по active_offers. Запечатан в audit chain.

## ROLLBACK
Per-file `*.bak.<ts>`. Дашборд: `server.py.bak.1784477795` (вернуть `host='0.0.0.0'`). Git HEAD `2540905`.

---

## MVCS SCORECARD (A3, после правок 2026-07-19)

| # | Objective | Статус | Примечание |
|---|---|---|---|
| 1 | Bounded spend cap | ✅ | 5 BNB/день + per-offer USD + rate + cooldown, на пути gate |
| 2 | Duplicate/nonce lock | ✅ | atomic nonce (BEGIN IMMEDIATE) + seaport counter |
| 3 | Action state machine | ✅ journaled (live-verified) | action_lifecycle: PROPOSED→VALIDATED→SUBMITTED / →REJECTED / →FAILED / CONFIRMED, append-only + sealed |
| 4 | Independent kill-switch | ✅ | fail-closed + доступ закрыт (localhost + TG-auth) |
| 5 | Dry-run / pre-live | ✅ | 3-слойный; сейчас DRY_RUN=0 (боевой, выбор оператора) |
| 6 | Final-state confirmation | ⏳ accepted-risk R2 | покупки ✅ (receipt); офферы — reconcile, не строгий confirm |
| 7 | Tamper-evident audit | ✅ | audit_sealer hash-chain (проверено детекцией подделки) |
| 8 | Exact intent binding | ⚠️ substitute R3 | блок-arm вместо per-action (HFT-несовместимо); компенсация ниже |
| 9 | Control-plane integrity | ✅* | дашборд закрыт, identity в seal; остаток: один хост, R4 |

## ACCEPTED-RISK REGISTER (§28: доп. controls как accepted risk с владельцем/датой)
- **R1** (state machine) — ✅ RESOLVED 2026-07-19: `action_lifecycle.py` журналит каждый переход (PROPOSED→VALIDATED→SUBMITTED / →REJECTED / →FAILED / CONFIRMED) через единый choke point `state.record_submit_event`, append-only, запечатано audit_sealer. Live-проверено на боте.
- **R2** (offer final-state) — owner Robert, review 2026-08-31. Компенсация: reconciliation против биржи; покупки подтверждаются receipt.
- **R3** (intent binding = блок-arm) — owner Robert, review 2026-08-31. **Обоснование:** per-action human approval несовместим с высокочастотным контр-биддером (остановит бота). Компенсирующие controls: bounded caps (R? §BUDGETS), закрытый control-plane (#4/#9), tamper-evident audit (#7), arm с expiry + actor. Это разрешённая §28 solo-редукция, НЕ снятие цели.
- **R4** (single-host control-plane + слабый пароль tormoz) — owner Robert, review 2026-08-31. Рекомендация: сменить пароль tormoz; рассмотреть внешний лимит (exchange sub-account) как настоящий out-of-process cap.

## TESTS / DEFINITION OF DONE (revalidation до снятия `Implemented, unverified`)
- [ ] Kill-switch drill: `/killswitch` при живом arm → подтвердить, что новые сабмиты блокируются (fail-closed).
- [ ] Nonce-race тест: два процесса на одном wallet+chain → нет двойного nonce.
- [ ] Offer final-state: сверка «submitted» vs реально принятые биржей за 24ч.
- [ ] `audit_sealer --verify` в CI/по расписанию — алерт при FAILED.
- [ ] Dry-run прогон после любого изменения critical control (§28: изменение gate/limit/kill-switch → назад в SIMULATE до ревалидации).
