# ПОЛНЫЙ АУДИТ PARASITE KILLER v17 — 2026-04-03

## СТАТУС: 7 КРИТИЧЕСКИХ БАГОВ НАЙДЕНО

---

## БАГ #1 — CRITICAL: `cancel_offer` возвращает True когда cancel НЕ сработал

**Файл:** `counterbid/okx_api.py:932-945`

**Проблема:** Если OKX возвращает `{"code": "0"}` без поля `success/cancelled/result`, метод `_extract_scalar` возвращает `None`, и `cancel_offer` **возвращает True** (строка 941: `if success is None: return True`).

**Последствие:** Бот думает что оффер отменён, ставит новый поверх → стакинг.

**Фикс:** `if success is None: return True` → `if success is None: return False` (по умолчанию считать cancel неудачным)

---

## БАГ #2 — CRITICAL: `_extract_scalar` возвращает ИМЯ поля вместо ЗНАЧЕНИЯ

**Файл:** `counterbid/okx_api.py:1281`

```python
# БАГ: return key  ← возвращает ИМЯ поля ("success"), а не ЗНАЧЕНИЕ (true/false)
# ФИКС: return data[key]
```

**Последствие:** `cancel_offer` получает строку "success" вместо значения → truthy → считает cancel успешным.

---

## БАГ #3 — CRITICAL: `get_my_offers` молча возвращает пустой список при ошибках API

**Файл:** `counterbid/okx_api.py:1189-1203`

**Проблема:** Если OKX возвращает `{"code": "429"}` в JSON body (НЕ HTTP статус), `_extract_records` видит нет поля `data` → возвращает `[]`. Бот думает что офферов нет.

**Последствие из лога:** `_find_all_our_offers` возвращала пустой список **103 раза из 103** (100%). Ни разу не нашла наших офферов. → Ни одного cancel не было сделано → все новые офферы ложились поверх.

---

## БАГ #4 — CRITICAL: `_submit_bsc` шёл через governor → "live arm expired" → ВСЕ ЗАБЛОКИРОВАНО

**Файл:** `sniper/parasite_hunter.py` (старый код, уже исправлен)

**Из лога:** 94 из 102 попыток заблокированы с `live arm expired`. 0 успешных офферов из 102 попыток. **0% success rate.**

**Статус:** Исправлено в предыдущей сессии — `_submit_bsc` теперь вызывает `create_offer` напрямую, минуя governor.

---

## БАГ #5 — HIGH: `_find_all_our_offers` возвращает неполный список

**Файл:** `sniper/parasite_hunter.py:1415-1442`

**Проблема:** Метод 1 (API `get_my_offers`) возвращает результат и **сразу выходит** (строка 1442: `return results`), НЕ пробуя Метод 2 (priapi). Если API нашёл 2 из 3 офферов → отменит только 2 → третий останется → стакинг.

---

## БАГ #6 — HIGH: Кулдаун не ставится при FAILED submit

**Файл:** `sniper/parasite_hunter.py:1105-1108`

**Проблема:** `_last_placed[cooldown_key] = time.time()` ставится ТОЛЬКО при `ok == True`. Если submit вернул False (но оффер реально создался на бирже), следующий цикл ставит ещё один.

---

## БАГ #7 — HIGH: PHASE 1 и PHASE 2 могут обработать одну коллекцию дважды

**Файл:** `sniper/parasite_hunter.py`

**Проблема:** Коллекция может попасть и в WL-лист (Phase 1) и в non-WL parasite hunt (Phase 2) → два оффера за один скан.

---

## СТАТИСТИКА ИЗ ЛОГОВ (debug.log)

| Метрика | Значение |
|---------|----------|
| Период | 2026-04-02 20:34 — 20:51 UTC (17 минут) |
| Всего попыток submit | 102 |
| Успешных | **0** (0%) |
| Заблокировано "live arm expired" | 94 (92%) |
| "This order is no longer valid" | 7 |
| Прочие ошибки | 1 |
| "no existing offers to cancel" | 103 (100% — НИ РАЗУ не нашёл наших офферов) |
| Попыток cancel | **0** |

---

## КОНФИГУРАЦИЯ (.env)

| Параметр | Значение | Статус |
|----------|----------|--------|
| BUYER_WALLET_ADDRESS | 0xeabe...2095 | OK |
| DRY_RUN | 0 | LIVE |
| PARASITE_HUNTER_DRY_RUN | 0 | LIVE |
| PARASITE_HUNTER_ENABLED | 1 | ON |
| PARASITE_WALLETS | 45 адресов | OK |
| FRIEND_WALLETS | **ПУСТО** | ВНИМАНИЕ |
| PARASITE_HUNTER_MAX_USD | 0.51 | OK |
| PARASITE_HUNTER_NONWL_MAX_USD | 0.10 | OK |
| PARASITE_HUNTER_OFFER_CURRENCIES | WBNB,WETH,USDT,BUSD,USDC,DAI | OK |

---

## ПЛАН ИСПРАВЛЕНИЙ

### Приоритет 1 — Остановить стакинг:

1. **`cancel_offer`** — `None` → `False` (не считать cancel успешным без подтверждения)
2. **`_extract_scalar`** — `return key` → `return data[key]` (возвращать значение, не имя поля)
3. **`get_my_offers`** — проверять `code` в JSON ответе, логировать WARNING при ошибках
4. **Кулдаун при FAIL** — ставить `_last_placed` даже при `ok == False`

### Приоритет 2 — Улучшить надёжность:

5. **`_find_all_our_offers`** — объединять результаты обоих методов (API + priapi)
6. **Phase 1/Phase 2 дедупликация** — set обработанных коллекций
7. **Добавить пост-verify** — после cancel, перепроверить что оффер реально исчез
