# Audit correction — v1 → v1.1

Preliminary v1 reported 345 candidates. During publication audit, one same-second ordering edge case was identified and removed:

- Binance NFT item: `#12061854`
- high-price record: message ID `1519142`
- low-price record: message ID `1519143`
- both export timestamps: `2026-08-11 00:16:45`

Because the high-price message ID precedes the low-price message ID, it does not satisfy the strict **low → later high** rule.

Corrected totals:

- 344 unique candidates
- 252 within 10 minutes
- 191 within 120 seconds
- 148 within 60 seconds
- median 87.5 seconds

This file exists to make the correction auditable.
