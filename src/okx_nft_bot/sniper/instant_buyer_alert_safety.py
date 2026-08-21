from __future__ import annotations

from functools import wraps
import json
import logging


log = logging.getLogger("sniper.buyer")


def install_instant_buyer_alert_safety(buyer_cls: type, chain_config: dict[str, dict]) -> None:
    """Make instant-buyer Telegram alerts single-attempt and provider-acknowledged.

    The historical buyer alert path attempted a direct POST and, on any local
    exception, retried through urllib. If the provider accepted the first POST
    but the response was lost, the fallback could duplicate the alert. R90
    replaces that transport behavior with one hardened request only.
    """
    original = buyer_cls._alert_buy_attempt
    if getattr(original, "_r90_single_attempt_alert", False):
        return

    @wraps(original)
    def guarded_alert_buy_attempt(self, attempt) -> None:
        if not self.tg_token or not self.tg_chat:
            return

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
            from okx_nft_bot.clients.http import StdlibHttpTransport

            transport = StdlibHttpTransport(
                timeout=10,
                max_retries=1,
                rate_limit_per_sec=5.0,
            )
            response = transport.request_json(
                method="POST",
                url=f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=json.dumps(
                    {
                        "chat_id": self.tg_chat,
                        "text": msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                ),
            )
            ok = response.get("ok")
            if ok is True:
                return
            if ok is False:
                log.warning("Telegram notify failed: telegram_rejected")
                return
            log.warning("Telegram notify failed: telegram_response_missing_boolean_ok")
        except Exception as exc:
            # Unknown delivery outcome must never trigger an automatic resend.
            log.warning("Telegram notify failed: %s", exc)

    guarded_alert_buy_attempt._r90_single_attempt_alert = True
    buyer_cls._alert_buy_attempt = guarded_alert_buy_attempt
