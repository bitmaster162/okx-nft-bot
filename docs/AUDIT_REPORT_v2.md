# OKX NFT Bot v13 — Аудит, оптимизация, документация
**Дата:** 2026-04-04

---

## 1. Аудит и исправления

### P0 — Критические (все исправлены)

**1. Мёртвые API-эндпоинты OKX (parasite_hunter.py)**
Старые эндпоинты `priapi/v5/nft/ec/offer/list` и `collection-offer/list` возвращали 404. Бот не видел офферы ни на одной коллекции.
*Исправление:* миграция на `priapi/v1/nft/order/offers` + парсинг SSR-страницы коллекции для `nftId`. Добавлена переменная `OKX_SLUG_MAP`.

**2. NameError в engine.py — `_retire_previous_offer_for_defense()`**
Метод использовал `action.chain`, но переменная `action` не была в scope. Защитный ребид крашил бот.
*Исправление:* добавлен параметр `chain: str = ""`, оба вызова обновлены.

**3. AttributeError в telegram_bot.py — `/parasitelive`**
Функция ожидала `str`, диспатчер передавал `list[str]`. Команда крашилась.
*Исправление:* сигнатура `args: list[str]` + безопасный доступ.

**4. Неинициализированный `_last_onchain_cancel_ts` (okx_api.py)**
Атрибут создавался только при первом cancel.
*Исправление:* `self._last_onchain_cancel_ts = 0.0` в `__init__()`.

**5. Утечки памяти — неограниченные кеши (parasite_hunter.py)**
`_last_placed`, `_collection_page_cache`, `_floor_cache` росли бесконечно.
*Исправление:* эвикция в `scan_once()` каждый цикл.

### P1 — Важные (все исправлены)

**6. N+1 запросы к API** — `_fetch_offers_by_nftid` вызывался 3+ раз на коллекцию.
*Исправление:* per-scan кеш `_nftid_offers_cache`.

**7. Stack overflow в `_coerce_price` (execution_governor.py)** — без лимита рекурсии.
*Исправление:* параметр `_depth` с лимитом 4.

**8. history_backfill cursor loop** — Проверено: защита `trade_pages >= 500` уже есть.

---

## 2. BULL BTC CLUB — почему бот не купил и не выставил

**Адрес:** `0xd27447bbe1d068909ffd920f60ca8f8c6d53a61c`
**Конфиг:** `max_buy_price=0.2205 WBNB`, `enabled=true`

### Причина 1: Нет слага в OKX_SLUG_MAP
После миграции API бот получает данные через страницу коллекции (нужен slug). BULL не был в `OKX_SLUG_MAP` → бот не мог получить `nftId` → не видел офферы.

**Исправлено:** `0xd27447bbe1d068909ffd920f60ca8f8c6d53a61c=bull-btc-club` добавлен в `.env`.

### Причина 2: Phase 3 (Missclick Buy) НЕ реализована
Бот работает только в режиме **Phase 1 — Undercutter**: выставляет/обновляет офферы ниже конкурентов. Логика прямой покупки при дешёвом листинге **отсутствует в коде**.

Даже если кто-то листит BULL дешевле 0.2205 WBNB — бот не купит. Он может только выставить collection offer и ждать акцепта от продавца.

### После рестарта бот сможет:
- Видеть офферы на BULL через новый API
- Выставлять undercut-офферы (при активном armlive)
- Показывать данные в `/parasite`, `/offers`, `/dashboard`

Для прямых покупок нужна реализация Phase 3.

---

## 3. Telegram-бот: все 38 команд

### Статус и информация
| Команда | Описание |
|---------|----------|
| `/help` | Список всех команд |
| `/status` | Общий статус бота |
| `/health` | Healthcheck: staleness, ошибки, задержки |
| `/writemetrics` | Записать снапшот метрик |

### Коллекции и рынок
| Команда | Описание |
|---------|----------|
| `/collections` | Реестр отслеживаемых коллекций |
| `/markets` | Кросс-маркет сводка |
| `/spreads [min_pct] [limit]` | Спреды между рынками |
| `/rankings [limit]` | Рейтинг коллекций |
| `/latest [n]` | Последние события (по умолч. 5) |

### Офферы и торговля
| Команда | Описание |
|---------|----------|
| `/offers <okx\|opensea> [coll] [limit]` | Офферы по маркету |
| `/massoffer <coll> <price> [rarity]` | Массовые per-item офферы |
| `/massofferstatus` | Статус mass-offer кампаний |
| `/massoffercancel` | Отмена активных mass-offer |

### Parasite Hunter
| Команда | Описание |
|---------|----------|
| `/parasite` | Статус + последний скан |
| `/parasitescan` | Немедленный скан |
| `/parasitelive on\|off` | DRY_RUN / LIVE переключение |
| `/parasitesales [n]` | Продажи с участием parasite |
| `/counterrun <coll>` | Dry-run скан одной коллекции |
| `/counterconfig <coll> <min> <max> [margin]` | Настройка execution |
| `/undercutstatus` | Статус undercutter |

### Execution
| Команда | Описание |
|---------|----------|
| `/armlive [min] [reason]` | Открыть LIVE-окно |
| `/disarmlive [reason]` | Закрыть LIVE-окно |
| `/killswitch` | Экстренная отмена + force dry-run |
| `/dashboard` | Дашборд: офферы, лимиты, статус |

### Алерты
| Команда | Описание |
|---------|----------|
| `/alertstatus` | Статус ack/snooze |
| `/alertack [note]` | Подтвердить алерт |
| `/alertsnooze [min] [reason]` | Заглушить на N минут |
| `/alertreset` | Сбросить ack/snooze |

### Циклы
| Команда | Описание |
|---------|----------|
| `/run <coll\|all> [trades\|listings]` | Запустить цикл |
| `/resetcursor <coll> [type]` | Сбросить курсор |
| `/sales` | Статистика продаж |
| `/sendalerts [min_pct] [limit]` | Аналитический отчёт |

### Профили и бекапы
| Команда | Описание |
|---------|----------|
| `/profiles` | Доступные профили |
| `/profile` | Текущий профиль |
| `/setprofile <dev\|stage\|prod>` | Установить профиль |
| `/backup [label]` | Бекап БД |
| `/backups [n]` | Список бекапов |
| `/restore <filename>` | Восстановить из бекапа |
