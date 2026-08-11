# Methodology — audited v1.1

## Source

- Telegram export: `ChatExport_2026-08-11.zip`
- Source SHA-256: `e7131bf441073ecb03ff1630e3741b17a89f38d5963959003c9d10662ec859f7`
- Inner file: `ChatExport_2026-08-11/result.json`
- Export title: `PURCHASED LOTS`
- Analyzed displayed-time window: Aug 1–11, 2026
- Currency restriction: USDT → USDT only.

## Detection rule

For each Binance NFT item ID:

1. identify a record priced at **≤ $0.20 USDT**;
2. identify a **later** record for the same item priced at **≥ $20 USDT**;
3. require the later record to occur within **24 hours**;
4. keep the shortest qualifying transition per item.

Order is audited using export timestamp and message ID. A same-second pair is accepted only when the high-price record has a later message ID.

## Audited result

- 8,856 USDT records in the analyzed window.
- 7,214 unique Binance NFT item IDs.
- **344** unique item IDs satisfy the strict rule.
- **252** satisfy it within 10 minutes.
- **191** within 120 seconds.
- **148** within 60 seconds.
- Median shortest interval: **87.5 seconds**.

## Audit correction from v1

The preliminary v1 scan counted 345 candidates. One same-second edge case (`item #12061854`) was removed because its high-price record had message ID `1519142` and the low-price record had later message ID `1519143`. The chronological direction required by the rule was therefore not established.

The corrected dataset is **344 candidates**. This correction is preserved rather than hidden.

## Evidence boundary

This rule detects anomalous same-item price sequences. It does not by itself establish beneficial ownership, coordination, intent, or that an account is Binance-owned. Those questions require internal account-linkage, KYC, device/IP, funding, order-matching, and surveillance evidence.
