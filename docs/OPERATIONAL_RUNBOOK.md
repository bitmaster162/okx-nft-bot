# Operational Runbook

Ежедневные операции с live-исполнением (BSC/ETH submit-путь).

Все команды запускаются с хоста; модификации состояния идут через контейнер
`okx-nft-bot-exec`. БД — `data/execution.sqlite3` смонтирована из хоста.

---

## 1. Продлить live-arm

Live-arm — временнóе окно, в котором `ExecutionGovernor` разрешает реальные
submit'ы. За его пределами любой submit блокируется с `live arm expired`, а в
`execution_submit_log` пишется `reason=governed_submit_failed`.

**Продление (24 часа):**
```bash
docker exec okx-nft-bot-exec python3 -m okx_nft_bot.execution_cli \
    arm-live --minutes 1440 --reason "<короткая-причина>"
```

Флаг — `--minutes` (целое). 24ч = 1440, 12ч = 720. При успехе печатается JSON
с `expires_at`. `force_dry_run` при этом автоматически выключается
(`live_arm:execution_cli`).

Рекомендуемый ритм — продлевать **вручную** по факту работы, не по крону
(крон превращает safety gate в always-on).

---

## 2. Бот не делает submit'ов — диагностика

**Симптом:** в `execution_submit_log` идут записи со `status=failed`,
`reason=governed_submit_failed`, но в transaction-логах кошелька ничего нет.

**Шаг 1.** Проверить окно live-arm:
```bash
python3 -c "
import sqlite3
c = sqlite3.connect('data/execution.sqlite3')
for k, v in c.execute(
    \"SELECT key, value FROM execution_runtime_state \"
    \"WHERE key IN ('live_armed_at','live_armed_until','force_dry_run')\"
):
    print(f'{k:<18} = {v}')
"
```

Если `live_armed_until` в прошлом или `force_dry_run='1'` — это причина.
Лечится `arm-live` (см. §1) или снятием killswitch.

**Шаг 2.** Подтверждение в логах:
```bash
docker logs --since 10m okx-nft-bot-exec 2>&1 | grep "place_single_offer BLOCKED"
```
Появится `live arm expired` / `execution_dry_run_enabled` — точная причина.

**Шаг 3.** Прочие возможные `reason` в `execution_submit_log`:
- `insufficient_balance:<TOKEN>` — пополнить кошелёк этой валютой.
- `mass_offer_engine_unavailable` — init движка упал, искать stacktrace в
  логах exec-контейнера.
- `unknown_currency:<TOKEN>` — валюта не прописана в `get_currency_address`.

---

## 3. Аварийная остановка live-исполнения

Два независимых контура, можно использовать по одному или оба:

**A. Мягко — закрыть live-окно (новых submit'ов не будет, активные офферы
остаются на площадке):**
```bash
docker exec okx-nft-bot-exec python3 -m okx_nft_bot.execution_cli \
    disarm-live --reason "emergency"
```

**B. Жёстко — остановить exec-контейнер (процесс kill, никаких дальнейших
действий со стороны бота):**
```bash
docker stop okx-nft-bot-exec
```

**Комбо (рекомендуется при подозрительной активности):** сначала `disarm-live`
(чтобы governor зафиксировал состояние в БД), затем `docker stop`. Обратный
порядок тоже работает, но тогда `disarm-live` пойдёт по commit'у БД уже после
остановки демона, что менее чисто.

**Возврат в строй:**
```bash
docker start okx-nft-bot-exec
# затем §1 для продления арма
```

Killswitch (`state.set_force_dry_run(True)`) — отдельный контур, активируется
через Telegram `/killswitch`. Снимается только вручную через sqlite:
`UPDATE execution_runtime_state SET value='0' WHERE key='force_dry_run'`.

---

## Ссылки на код

- `src/okx_nft_bot/execution_cli.py` — CLI (`arm-live`, `disarm-live`, `audit-state`, …)
- `src/okx_nft_bot/undercutter/state.py:637` — `PositionState.arm_live()`
- `src/okx_nft_bot/mass_offer/engine.py:280` — `place_single_offer()` (точка блокировки)
- `src/okx_nft_bot/sniper/parasite_hunter.py:2797` — вызывающая сторона BSC-submit

---

## История изменений

- **2026-04-21**: `PARASITE_HUNTER_OFFER_CURRENCIES` сужен до `WBNB,USDT,BUSD` (BSC-native only). Удалены `WETH,USDC,DAI` — ETH-ликвидность не фондирована, попытки офферов в этих валютах генерировали ~92% шума в `execution_submit_log` (~20k `insufficient_balance:WETH` за 2ч). Применяется при следующем рестарте `okx-nft-bot-exec`.
