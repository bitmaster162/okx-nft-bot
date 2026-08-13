from __future__ import annotations

from dataclasses import dataclass

from okx_nft_bot.undercutter.defense_fanout_safety import install_defense_fanout_safety


@dataclass
class _Action:
    action_type: str
    collection: str
    executed: bool = False
    error: str | None = None


class _Engine:
    def __init__(self, actions: list[_Action]) -> None:
        self.actions = actions
        self.applied: list[_Action] = []

    def run_cycle(self):
        for action in self.actions:
            self._apply_action(action)
        return self.actions

    def _apply_action(self, action: _Action) -> None:
        self.applied.append(action)
        action.executed = True


def test_duplicate_defense_is_suppressed_within_one_cycle() -> None:
    install_defense_fanout_safety(_Engine)
    first = _Action("DEFENSE", "0xABC")
    second = _Action("DEFENSE", "0xabc")
    engine = _Engine([first, second])

    result = engine.run_cycle()

    assert result == [first, second]
    assert engine.applied == [first]
    assert first.executed is True
    assert second.executed is False
    assert second.error == "duplicate defense suppressed for collection in current cycle"


def test_different_collections_each_get_one_defense() -> None:
    install_defense_fanout_safety(_Engine)
    first = _Action("DEFENSE", "0xaaa")
    second = _Action("DEFENSE", "0xbbb")
    engine = _Engine([first, second])

    engine.run_cycle()

    assert engine.applied == [first, second]
    assert first.executed is True
    assert second.executed is True


def test_non_defense_actions_are_never_suppressed() -> None:
    install_defense_fanout_safety(_Engine)
    first = _Action("WITHDRAW", "0xaaa")
    second = _Action("WITHDRAW", "0xaaa")
    engine = _Engine([first, second])

    engine.run_cycle()

    assert engine.applied == [first, second]


def test_guard_resets_between_cycles() -> None:
    install_defense_fanout_safety(_Engine)
    action = _Action("DEFENSE", "0xaaa")
    engine = _Engine([action])

    engine.run_cycle()
    action.executed = False
    engine.run_cycle()

    assert engine.applied == [action, action]
    assert action.executed is True


def test_direct_apply_outside_cycle_preserves_legacy_behavior() -> None:
    install_defense_fanout_safety(_Engine)
    first = _Action("DEFENSE", "0xaaa")
    second = _Action("DEFENSE", "0xaaa")
    engine = _Engine([])

    engine._apply_action(first)
    engine._apply_action(second)

    assert engine.applied == [first, second]


def test_installer_is_idempotent() -> None:
    install_defense_fanout_safety(_Engine)
    run_cycle = _Engine.run_cycle
    apply_action = _Engine._apply_action

    install_defense_fanout_safety(_Engine)

    assert _Engine.run_cycle is run_cycle
    assert _Engine._apply_action is apply_action
