from __future__ import annotations

import okx_nft_bot.ops as ops
from okx_nft_bot.undercutter.state import PositionState


def test_position_state_is_available_to_runtime_metrics_module() -> None:
    """Regression: build_runtime_metrics uses PositionState outside local helpers."""
    assert ops.PositionState is PositionState
