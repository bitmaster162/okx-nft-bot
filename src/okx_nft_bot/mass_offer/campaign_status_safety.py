from __future__ import annotations

from functools import wraps
import logging
from typing import Any


log = logging.getLogger("mass_offer.campaign_status_safety")


def install_mass_offer_campaign_status_safety(tracker_class: type[Any]) -> None:
    """Prevent a campaign from becoming terminal while active items remain.

    Mass-offer cancellation can target only part of a campaign.  The legacy
    engine marks every touched campaign ``cancelled`` after any successful item
    cancellation, even when sibling item rows remain ``active``.  Keep the
    campaign-level status non-terminal until its local item ledger has no active
    rows.  This changes bookkeeping only; it does not add or modify marketplace
    cancellation, retry, readback, RPC, or on-chain effects.
    """
    current = tracker_class.mark_campaign_status
    if getattr(current, "_r63_campaign_status_guard", False):
        return

    original = current

    @wraps(original)
    def guarded_mark_campaign_status(
        self: Any,
        *,
        campaign_id: int,
        status: str,
    ) -> None:
        if str(status) != "cancelled":
            return original(self, campaign_id=campaign_id, status=status)

        with self._connect() as conn:
            active_row = conn.execute(
                """
                SELECT 1
                FROM mass_offer_items
                WHERE campaign_id = ? AND status = 'active'
                LIMIT 1
                """,
                (int(campaign_id),),
            ).fetchone()

        if active_row is not None:
            log.info(
                "campaign %s remains non-terminal: active mass-offer items still exist",
                campaign_id,
            )
            return None

        return original(self, campaign_id=campaign_id, status=status)

    guarded_mark_campaign_status._r63_campaign_status_guard = True
    tracker_class.mark_campaign_status = guarded_mark_campaign_status
