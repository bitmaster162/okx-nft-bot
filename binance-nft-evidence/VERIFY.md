# Verify the Binance NFT evidence — v1.1

Do not rely on the infographic alone.

## 30-second verification

1. Open [`data/TOP20.csv`](data/TOP20.csv).
2. Pick any `item_id`.
3. Compare `low_price_usdt`, `high_price_usdt`, export timestamps, Telegram message IDs and account labels.
4. Open the `binance_item_url` in that row.
5. Read [`METHODOLOGY.md`](METHODOLOGY.md) for the strict low→later-high rule.
6. Inspect `data/ALL_344_part01.csv` through `part05.csv` for the complete normalized set.
7. Check [`PUBLIC_MIRROR_MANIFEST.sha256`](PUBLIC_MIRROR_MANIFEST.sha256) for integrity references.
8. Read [`BINANCE_MARKET_SURVEILLANCE_2023.md`](BINANCE_MARKET_SURVEILLANCE_2023.md) for Binance's own primary-source NFT wash-trading standard.
9. Read [`HISTORY_ALPINE_RACE_2026-03.md`](HISTORY_ALPINE_RACE_2026-03.md) for the separately classified March 2026 historical continuity evidence.

## Example

Denmark #47065203:

- low record: `$0.11`, `Arshad-1221`, `2026-08-08 22:18:11`, message `1518331`
- later high record: `$45`, `Fan-Token-4`, `2026-08-08 22:18:38`, message `1518333`
- interval: `27 seconds`
- Binance item: https://www.binance.com/ru/nft/item/47065203

## Evidence hierarchy

- **Primary-source verified:** Binance Market Surveillance emails from 17 February and 10 March 2023.
- **Dataset-derived:** August 2026 `PURCHASED LOTS` export and strict 344-candidate audit.
- **Historical continuity:** March 2026 Alpine Race Collectibles video-backed archive analysis; not promoted to independently verified marketplace data.

## What the evidence supports

A repeatable set of anomalous same-item price sequences, recurring account-label patterns and cyclic behavior consistent with indicators that warrant an independent wash-trading / artificial-volume investigation.

## What it does not prove

It does not establish beneficial ownership of the accounts, Binance ownership of those accounts/bots, coordination intent, KYC linkage, device/IP linkage or internal order-matching behavior.

Those determinations require Binance-controlled evidence.
