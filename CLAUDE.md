# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
pip install -e .
pip install -e ".[dev]"   # includes pytest

# Run tests
pytest

# Run CLI
okx-nft-bot <command>     # 50+ subcommands, see cli.py

# Key daemon commands
okx-nft-bot run-daemon          # main monitoring loop
okx-nft-bot sales-stream        # streaming sales events
okx-nft-bot run-execution-daemon
```

## Architecture

**OKX NFT Bot** monitors NFT markets (OKX, OpenSea, MagicEden), detects arbitrage opportunities, and executes buy/offer/bid operations.

### Data Flow

```
Market API → Provider → normalize → RuleFilter → Notifier (Telegram/Webhook)
                ↓
          SQLiteStore (events, cursors, state)
                ↓
         ExecutionEngine → Blockchain (buy/offer)
         AnalyticsEngine (spreads, PnL, rankings)
```

### Key Modules (`src/okx_nft_bot/`)

| Module | Purpose |
|---|---|
| `cli.py` | Click CLI entry point with 50+ subcommands |
| `models.py` | Core types: `RawEvent`, `NFTEvent`, `FilterDecision`, `DeliveryResult` |
| `config.py` | `Settings` dataclass (~100 env vars, loaded from `.env`) |
| `registry.py` | `CollectionRegistry` — loads `config/collections_registry.json` |
| `scheduler.py` | `MultiCollectionRunner` daemon |
| `clients/` | API clients: `OKXMarketplaceClient`, `OpenSeaClient`, `MagicEdenClient` |
| `providers/` | Per-market event providers (trades, listings, offers) |
| `normalizers/` | Convert market-specific payloads → standard `NFTEvent` |
| `pipeline/` | Live cycle orchestration, `run_once()` logic |
| `storage/` | `SQLiteStore` (events, cursors), `OffersStore`, `FraudStore` |
| `rules/` | `RulePack` evaluation — applies filter rules to events |
| `notifiers/` | `TelegramNotifier`, `WebhookNotifier`, `FanoutNotifier` |
| `execution/` | `ExecutionGovernor` — runtime caps (gas, USD, per-hour limits) |
| `sniper/` | `Buyer` (on-chain purchase), `ParasiteHunter`, `OfferBlaster` |
| `undercutter/` | `UndercutterEngine` — auto-undercut competitor offers |
| `counterbid/` | `CounterBidEngine` — counter-bid strategy |
| `mass_offer/` | `MassOfferEngine` — bulk offer creation |
| `analytics/` | `detect_spreads()`, `rank_collections()`, `CrossMarketAnalytics` |
| `fraud/` | Fraud detection & wallet scoring |
| `signing/` | `SeaportSigner` for OpenSea on-chain signing |
| `pnl/` | PnL tracking engine |

### State Management

- Cursor-based pagination: each market/source_mode stores its cursor in SQLite `state` table
- Three main databases: `data/okx_nft_bot.sqlite3`, `data/offers.sqlite3`, `data/execution.sqlite3`
- Providers and normalizers are stateless; all mutable state lives in SQLite

### Configuration Files

- `.env` — API keys, wallet private key, RPC URLs (never commit)
- `config/collections_registry.json` — collections to monitor (name, market, chain, slug, address)
- `config/rule_packs.json` — alert filtering rules (event types, markets, min_price/volume thresholds)
- `config/sniper_config.json` — buyer strategy parameters
- `config/pnl_config.json` — PnL tracking parameters

### Patterns

- **Market abstraction**: all markets implement the same Provider interface, output normalized `NFTEvent`
- **Dataclass/Pydantic mix**: `@dataclass(slots=True)` for performance-critical paths; Pydantic for validation
- **Rate limiting**: global per-market limiters shared across clients via `StdlibHttpTransport`
- **HTTP**: uses `curl_cffi` (Cloudflare bypass capable) not `requests`
- **Factory functions**: `build_notifier()` and client factories called in CLI command handlers
