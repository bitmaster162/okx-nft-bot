## Известные проблемы (на момент старта аудита 2026-04-19)

### ✅ FIXED
- offer_blaster.py:269 — ETH Seaport v1.5 → v1.6 (commit 7f7dd7e)
- **K4**: offer_blaster.py:559 — EIP-712 domain version "1.5" → "1.6" (commit 763baf9)
  Sibling of 7f7dd7e: address was updated but domain version string was stale → signatures
  would fail domain-hash check on v1.6 contract. Now consistent with clients/opensea.py
  and signing/seaport_signer.py.

### 🔴 CONFIRMED, NOT FIXED

**K1: Дубли офферов при рассинхроне _find_all_our_offers**
- parasite_hunter.py:2409
- Method 3 (локальный трекер) срабатывает ТОЛЬКО если оба API вернули 0
- Если API вернул частичный/чужой результат, локальные ID не мержатся →
  защита считает что наших офферов нет → создаёт дубль
- Фикс: мержить локальный трекер всегда, не только при пустом API
- P1

**K2: Ghost-branch proceed при неполной он-чейн отмене**
- parasite_hunter.py:2539-2542
- Даёт proceed если ok_count == len(all_ours) и API видит те же order_id
- Трактуется как stale-кеш OKX, но если реальная отмена он-чейн пошла
  не до конца — в UI будет дубль
- Фикс: добавить он-чейн верификацию (RPC call на canceled event) перед proceed
- P2

**K3: Cooldown _PLACE_COOLDOWN теряется при рестарте**
- parasite_hunter.py:329
- _last_placed в памяти процесса, после рестарта сброс в 0
- При рестарте контейнера (частое событие) все guards обнуляются →
  потенциальный burst дублей сразу после рестарта
- Фикс: персистировать _last_placed в SQLite или Redis
- P2

### 🟡 SUSPEND (WIP uncommitted changes — не откатывать, но опасные)

**K5: orderType 2 → 3 в seaport_signer.py:287 (WIP)**
- Uncommitted: `OrderType.FULL_RESTRICTED` (2) → `OrderType.PARTIAL_RESTRICTED` (3)
- Комментарий в коде: `# ordertype_okx_partial_restricted ??? OKX expects 3, not 2` —
  знак "???" означает что автор правки сам не уверен
- Противоречивые свидетельства в репо:
  - parasite_hunter.py:2116 комментарий: `# 3 = collection offer`
    (т.е. 3 — для criteria-based collection offers, не для item offers)
  - counterbid/okx_api.py:1469 дефолтит в 2: `int(params.get("orderType", 2))`
  - clients/opensea.py:313 использует 2 с комментарием `FULL_RESTRICTED`
  - offer_blaster.py:504 использует 0 (FULL_OPEN)
- `PARTIAL_RESTRICTED` — для ордеров с criteria resolver (коллекционные). Если
  `_build_offer_payload` формирует item-offers, контракт/OKX отклонит
- Проверка: запросить у OKX API принятый accepted-order и посмотреть какой
  orderType они возвращают для item-offers. Либо откатить на 2 и поставить обе
  ветки под feature-flag
- P1 (блокирует real-order path если 3 неправильный)

**K6: Удалён _buyer_lock (threading.Lock) в parasite_hunter.py (WIP)**
- Uncommitted: удалён `self._buyer_lock = threading.Lock()` и оба `with self._buyer_lock:`
  блока в `_execute_buy_config`
- Исходный комментарий: "protects _buyer.dry_run mutations"
- Без лока: если `_execute_buy_config` когда-либо вызывается из нескольких потоков
  (или параллельно с другим buy-path на том же buyer), race на
  read→mutate→restore последовательности:
  - Thread A: original=False, ставит True, вызывает try_buy
  - Thread B: original=True (уже заменён A!), ставит True
  - A: restore→False (ок)
  - B: restore→True (залипло, реальный buy теперь идёт в dry_run)
  — или наоборот: реальный buy проходит там где ожидался dry_run
- Фикс: вернуть lock. Если гарантированно single-threaded по всем call sites —
  убрать сам механизм подмены, а не только lock
- P1 (регрессия — теряется safety для AUTO_BUY_CONFIG_DRY_RUN)

**K7: Удалён min_listing_price_bnb floor в parasite_hunter.py (WIP)**
- Uncommitted: удалено чтение `sell_settings.min_listing_price_bnb` и обе clamp-точки
  (старые строки ~3298 и ~3310)
- Теперь sell-цена опирается только на `low_price` (расчётный пол). Если
  low_price миграция/стейл-данные сделают его аномально низким — листинги уйдут
  дёшево без глобального страховочного пола
- Нужно подтверждение: удаление намеренное (config-driven floor устарел)
  или забытая часть рефакторинга?
- P2

**K8: Двойной reconcile_active_offers за цикл в execution_daemon.py (WIP)**
- Uncommitted: добавлен `[RECONCILE-EARLY]` в начало cycle + существующий
  `[RECONCILE]` после UNDERCUT фазы
- Результат: 2× вызова exchange API на reconcile за каждый cycle → удвоение
  нагрузки на rate-limit (и так упираемся в 429 по Q1)
- Не баг сам по себе, но усугубляет Q1
- Фикс: оставить только один reconcile (либо в начале, либо в конце), либо
  сделать rate-limited (reconcile не чаще чем раз в N секунд)
- P2

**K9: reconcile_active_offers хардкодит chain="bsc" (WIP)**
- Uncommitted block в execution_daemon.py передаёт `chain="bsc"` в обе точки
- Если бот работает и на ETH (а он работает — K4/Seaport ETH v1.6 фикс), state
  drift на ETH-стороне никогда не видится → stale local state для ETH offers
- Фикс: итерировать по всем активным цепям из registry или settings
- P2

### 🟠 OPEN QUESTIONS (не добавлять в аудит, но держать в уме)

**Q1: Rate limit 429 от OKX на 40% запросов**
- Даже при OKX_RATE_LIMIT_PER_SEC=0.3 продолжается 429 (50011)
- Гипотеза: 3 контейнера × свой лимитер на процесс = overshoot
- Решение: либо IPC-lock между контейнерами, либо второй сервер с отдельным IP

**Q2: CR7-коллекция — были офферы выше floor?**
- Kimi наставил дублей, потом юзер вручную отменил
- Нужна ретроспектива в логах: были ли submit > floor_price
- Сейчас USDT=0 на кошельке, новых офферов нет

**Q3: wash-trading детектор**
- 0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7 ↔ 0x660bfc61854a8cc6fa0bd03b1331fd85709af7dc
  гоняют NFT между собой (скрин ByteVault)
- Оба попали в PARASITE_WALLETS — видимо ошибочно для этих
- Нужна логика: если две pair-wallet из списка часто матчатся между собой
  на одной коллекции → флаг wash, не конкурировать там

**Q4: r/s signature split при submit_seaport_order (WIP)**
- Uncommitted в counterbid/okx_api.py:1234-1239
- До: `r=""`, `s=""` (пустые поля), `signature=<full 65-byte hex>`
- После: `r = sig[0:32]`, `s = sig[32:64]`, `v` отбрасывается (байт 64)
- Поле `signature` всё ещё отправляется полным → OKX может использовать либо
  r/s либо signature. Но:
  - Стандартный Seaport подпись 65 байт (r|s|v)
  - EIP-2098 compact — 64 байта (r|yParityAndS), v закодирован в старший бит s
  - Если OKX ожидает EIP-2098, то просто split без переноса v в s — сломан
  - Если OKX ожидает (r, s, v отдельно) — мы не шлём v
- Нужно проверить: принимает ли OKX новые ордера с этим изменением в продакшене?
  Если нет — либо откатить (r="", s=""), либо правильно посчитать yParityAndS
