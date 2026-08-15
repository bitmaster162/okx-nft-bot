from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("sniper.buyer")


def install_instant_buy_alert_safety(
    buyer_cls: type,
    chain_config: dict[str, dict[str, Any]],
) -> None:
    """Make legacy instant-buy Telegram alerts single-attempt and provider-acked."""
    original = buyer_cls._alert_buy_attempt
    if getattr(original, "_r90_single_attempt_alert_guard", False):
        return

    def safe_alert_buy_attempt(self, attempt) -> None:
        if not self.tg_token or not self.tg_chat:
            return

        # Keep the existing in-memory dedup behavior for stale listing errors.
        if attempt.error and "already failed (stale" in attempt.error:
            return

        chain_cfg = chain_config.get(attempt.chain, {})
        explorer = chain_cfg.get("explorer", "")

        if attempt.success:
            emoji = "✅"
            status = "BOUGHT"
        elif attempt.dry_run:
            emoji = "🏷️"
            status = "DRY RUN"
        else:
            emoji = "❌"
            status = f"FAILED: {attempt.error}"

        tx_line = ""
        if attempt.tx_hash:
            tx_line = f"\nTX: <a href='{explorer}{attempt.tx_hash}'>{attempt.tx_hash[:16]}...</a>"

        msg = (
            f"{emoji} <b>Auto-Buy {status}</b>\n"
            f"Collection: {attempt.collection_name}\n"
            f"Token: #{attempt.token_id}\n"
            f"Price: {attempt.listing_price:.6f} {attempt.currency}\n"
            f"Max: {attempt.max_buy_price:.6f} {attempt.currency}\n"
            f"Chain: {attempt.chain.upper()}\n"
            f"Latency: {attempt.latency_ms}ms"
            f"{tx_line}"
        )

        try:
            from okx_nft_bot.sales_stream import http

            response = http.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            ok = payload.get("ok") if isinstance(payload, dict) else None
            if ok is True:
                return
            if ok is False:
                description = payload.get("description", "unknown rejection") if isinstance(payload, dict) else "unknown rejection"
                log.warning("Telegram notify rejected by provider: %s", description)
                return
            log.warning("Telegram notify malformed response: missing boolean 'ok'")
        except Exception as exc:
            # Effectful notifications are single-attempt. A transport exception can
            # have an unknown provider outcome, so never issue a fallback POST here.
            log.warning("Telegram notify failed after single attempt: %s", exc)

    safe_alert_buy_attempt._r90_single_attempt_alert_guard = True
    buyer_cls._alert_buy_attempt = safe_alert_buy_attempt
