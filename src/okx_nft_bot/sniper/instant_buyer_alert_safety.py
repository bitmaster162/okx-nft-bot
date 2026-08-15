from __future__ import annotations

from functools import wraps
import json
from typing import Any

from okx_nft_bot.clients.http import StdlibHttpTransport


def install_instant_buyer_alert_safety(buyer_cls: type) -> None:
    """Make specialized instant-buy Telegram alerts single-attempt and ack-aware.

    The legacy buyer alert retried the same effect through urllib after an
    exception from its first HTTP client. A response-loss after provider
    acceptance could therefore deliver the same alert twice. This installer
    preserves the existing specialized HTML message while using one transport
    attempt and requiring Telegram's provider-level ``ok`` acknowledgement.
    """
    original = buyer_cls._alert_buy_attempt
    if getattr(original, "_r90_instant_buyer_alert_safety", False):
        return

    source_globals = getattr(original, "__globals__", {})
    chain_config = source_globals.get("CHAIN_CONFIG", {})
    log = source_globals.get("log")

    @wraps(original)
    def safe_alert(self: Any, attempt: Any) -> None:
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

        payload = {
            "chat_id": self.tg_chat,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        transport = StdlibHttpTransport(
            timeout=10,
            max_retries=1,
            rate_limit_per_sec=5.0,
        )
        try:
            response = transport.request_json(
                method="POST",
                url=f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=json.dumps(payload),
            )
        except Exception as exc:
            if log is not None:
                log.warning("Telegram notify failed: %s", exc)
            return

        ok = response.get("ok")
        if ok is True:
            return
        if ok is False:
            if log is not None:
                log.warning(
                    "Telegram notify rejected by provider: %s",
                    response.get("description") or "unknown rejection",
                )
            return
        if log is not None:
            log.warning("Telegram notify failed: Telegram Bot API response missing boolean 'ok'")

    safe_alert._r90_instant_buyer_alert_safety = True
    buyer_cls._alert_buy_attempt = safe_alert
