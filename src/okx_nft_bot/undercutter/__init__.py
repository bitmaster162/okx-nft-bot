from __future__ import annotations

from okx_nft_bot.undercutter.engine import UndercutAction, UndercutEngine
from okx_nft_bot.undercutter.monitor import OfferMonitor
from okx_nft_bot.undercutter.state import ActiveOffer, PositionState
from okx_nft_bot.undercutter.strategy import AttackTarget, UndercutStrategy
from okx_nft_bot.undercutter.defense_fanout_safety import install_defense_fanout_safety

install_defense_fanout_safety(UndercutEngine)

__all__ = [
    "ActiveOffer",
    "AttackTarget",
    "OfferMonitor",
    "PositionState",
    "UndercutAction",
    "UndercutEngine",
    "UndercutScheduler",
    "UndercutStrategy",
]


def __getattr__(name: str):
    if name == "UndercutScheduler":
        from okx_nft_bot.undercutter.scheduler import UndercutScheduler

        return UndercutScheduler
    raise AttributeError(name)
