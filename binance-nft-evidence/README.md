# Binance NFT market-integrity evidence — public mirror v1.1

This directory is an isolated **evidence-only public mirror**. It does not modify or describe the OKX NFT bot runtime contained elsewhere in this repository.

## Audited result — Aug 1–11, 2026

Strict rule: same Binance NFT item has a USDT record at **≤ $0.20**, followed by a **later** USDT record at **≥ $20 within 24 hours**. Same-second ordering is resolved by Telegram export message ID.

- **344** unique NFT item IDs satisfy the rule.
- **252** within **10 minutes**.
- **191** within **120 seconds**.
- **148** within **60 seconds**.
- Median shortest interval: **87.5 seconds**.
- `Arshad-1221`: **111** USDT records, all exactly **$0.11**.
- `Fan-Token-4`: **143** USDT records; median **$45**; **86** records ≥$20.
- `Arshad-1221 → Fan-Token-4`: **52** unique item IDs under the 24h rule.

## Direct verification examples

- Denmark #47065203 — $0.11 → $45 in 27 sec — https://www.binance.com/ru/nft/item/47065203
- Croatia #48967209 — $0.11 → $28 in 36 sec — https://www.binance.com/ru/nft/item/48967209
- Cameroon #59042534 — $0.11 → $45 in 78 sec — https://www.binance.com/ru/nft/item/59042534
- Qatar #42361638 — $0.11 → $45 in 82 sec — https://www.binance.com/ru/nft/item/42361638
- Croatia #48106439 — $0.11 → $47 in 135 sec — https://www.binance.com/ru/nft/item/48106439
- Australia #46930030 — repeating $0.11/$45 cycle — https://www.binance.com/ru/nft/item/46930030
- Japan #48142250 — $0.11 → $45 → $0.11 → $6 → $46 — https://www.binance.com/ru/nft/item/48142250
- MEKACORNO U9 #56177 — $0.20 → $20.0869 in 5 sec — https://www.binance.com/ru/nft/item/56177
- T-Mac Time #12073589 — $0.15 → $25.1384 in 10 sec — https://www.binance.com/ru/nft/item/12073589
- APOKI #975397 — $0.18 → $21.32 in 23 sec — https://www.binance.com/ru/nft/item/975397

## Files

- `data/TOP20.csv` — high-signal audited examples.
- `data/ALL_344_part01.csv` … `part05.csv` — complete audited candidate set.
- `data/TOP20_RAW_TELEGRAM_RECORDS.json` — raw export records for TOP20 examples.
- `data/ACCOUNT_CLUSTER_SUMMARY_v1_1.json` — recurring label statistics.
- `METHODOLOGY.md` — reproducible detection rule.
- `AUDIT_CORRECTION_v1_1.md` — documented 345→344 publication correction.
- `PUBLIC_MANIFEST.sha256` — integrity references.

## Evidence boundary

This dataset supports a claim of **repeatable anomalous same-item price patterns consistent with indicators that warrant a wash-trading / artificial-volume investigation**.

It does **not** establish that any account is owned by Binance, that the accounts are Binance-operated bots, or beneficial ownership/intent. Those questions require Binance-controlled KYC, account-linkage, IP/device, funding, order-matching and surveillance evidence.

Binance Academy wash-trading reference:
https://academy.binance.com/en/glossary/wash-trading
