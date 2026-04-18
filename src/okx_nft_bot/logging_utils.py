from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from typing import Any


def log_event(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str), file=sys.stderr)
