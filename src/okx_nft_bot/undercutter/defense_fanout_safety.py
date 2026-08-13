from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MARKER = "_r64_defense_fanout_guard"


def install_defense_fanout_safety(engine_class: type[Any]) -> None:
    """Limit one DEFENSE action per collection within a single run cycle.

    A failed retirement can legitimately leave more than one live offer for the
    same collection in local state. The legacy cycle iterates every active row,
    so those duplicate rows can otherwise fan out into multiple replacement
    DEFENSE actions in the same cycle. This guard preserves all exposure rows
    but suppresses duplicate DEFENSE execution for the collection until the next
    cycle.
    """

    if getattr(engine_class, _MARKER, False):
        return

    original_run_cycle = engine_class.run_cycle
    original_apply_action = engine_class._apply_action
    local = threading.local()

    def _stack() -> list[tuple[int, set[str]]]:
        stack = getattr(local, "stack", None)
        if stack is None:
            stack = []
            local.stack = stack
        return stack

    def guarded_run_cycle(self: Any, *args: Any, **kwargs: Any):
        stack = _stack()
        frame = (id(self), set())
        stack.append(frame)
        try:
            return original_run_cycle(self, *args, **kwargs)
        finally:
            if stack and stack[-1] is frame:
                stack.pop()
            else:
                try:
                    stack.remove(frame)
                except ValueError:
                    pass

    def guarded_apply_action(self: Any, action: Any) -> None:
        if getattr(action, "action_type", None) != "DEFENSE":
            original_apply_action(self, action)
            return

        collection = str(getattr(action, "collection", "") or "").strip().lower()
        if not collection:
            original_apply_action(self, action)
            return

        seen: set[str] | None = None
        for owner_id, collections in reversed(_stack()):
            if owner_id == id(self):
                seen = collections
                break

        if seen is None:
            original_apply_action(self, action)
            return

        if collection in seen:
            action.executed = False
            action.error = "duplicate defense suppressed for collection in current cycle"
            logger.warning(
                "Suppressed duplicate DEFENSE for %s in the same undercutter cycle",
                collection,
            )
            return

        seen.add(collection)
        original_apply_action(self, action)

    guarded_run_cycle.__name__ = original_run_cycle.__name__
    guarded_run_cycle.__doc__ = original_run_cycle.__doc__
    guarded_apply_action.__name__ = original_apply_action.__name__
    guarded_apply_action.__doc__ = original_apply_action.__doc__
    setattr(guarded_run_cycle, _MARKER, True)
    setattr(guarded_apply_action, _MARKER, True)

    engine_class.run_cycle = guarded_run_cycle
    engine_class._apply_action = guarded_apply_action
    setattr(engine_class, _MARKER, True)
