from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.sniper.parasite_hunter import ParasiteHunter


class _FakeGovernor:
    def __init__(self, *, effective_dry_run: bool) -> None:
        self._effective_dry_run = effective_dry_run

    def effective_dry_run(self, configured_dry_run: bool) -> bool:
        _ = configured_dry_run
        return self._effective_dry_run


class _FakeOfferState:
    def __init__(self, order_hash: str) -> None:
        self._order_hash = order_hash

    def get_active_offers(self, *, chain: str):
        _ = chain
        return [SimpleNamespace(order_hash=self._order_hash, collection='0xabc')]


class _FakeEngine:
    def __init__(self, *, effective_dry_run: bool, place_result: bool, order_hash: str = 'offer-1') -> None:
        self.governor = _FakeGovernor(effective_dry_run=effective_dry_run)
        self.state = _FakeOfferState(order_hash=order_hash)
        self.place_result = place_result
        self.calls: list[dict[str, object]] = []

    def place_single_offer(self, **kwargs):
        self.calls.append(kwargs)
        return self.place_result


def _hunter(monkeypatch) -> ParasiteHunter:
    monkeypatch.delenv('OPENSEA_API_KEY', raising=False)
    monkeypatch.delenv('PARASITE_HUNTER_ENABLED', raising=False)
    monkeypatch.delenv('PARASITE_HUNTER_DRY_RUN', raising=False)
    return ParasiteHunter({}, {'buy_settings': {'enabled': False}})


def test_submit_bsc_respects_execution_dry_run(monkeypatch) -> None:
    hunter = _hunter(monkeypatch)
    fake_engine = _FakeEngine(effective_dry_run=True, place_result=True)
    events: list[dict[str, object]] = []

    hunter._get_bsc_engine = lambda: fake_engine
    hunter._get_currency_address = lambda currency, chain: '0xwbnb'
    hunter._check_balance_for_offer = lambda *_args, **_kwargs: True
    hunter._record_execution_submit_event = lambda **payload: events.append(payload)

    ok = hunter._submit_bsc('0xabc', '1', 0.25, 'WBNB', quantity=1, duration_hours=24)

    assert ok is False
    assert fake_engine.calls == []
    assert events[-1]['status'] == 'blocked'
    assert events[-1]['reason'] == 'execution_dry_run_enabled'


def test_submit_bsc_tracks_latest_governed_offer(monkeypatch) -> None:
    hunter = _hunter(monkeypatch)
    fake_engine = _FakeEngine(effective_dry_run=False, place_result=True, order_hash='offer-42')

    hunter._get_bsc_engine = lambda: fake_engine
    hunter._get_currency_address = lambda currency, chain: '0xwbnb'
    hunter._check_balance_for_offer = lambda *_args, **_kwargs: True
    hunter._record_execution_submit_event = lambda **_payload: None

    ok = hunter._submit_bsc('0xabc', '7', 0.33, 'WBNB', quantity=2, duration_hours=48)

    assert ok is True
    assert fake_engine.calls == [{
        'collection_address': '0xabc',
        'token_id': '7',
        'price_wbnb': 0.33,
        'currency_address': '0xwbnb',
        'chain': 'bsc',
        'duration_hours': 48,
        'dry_run': False,
        'quantity': 2,
    }]
    assert hunter._local_placed_offers['0xabc:bsc'] == ['offer-42']


def test_submit_eth_is_blocked_until_governed_runtime(monkeypatch) -> None:
    hunter = _hunter(monkeypatch)
    events: list[dict[str, object]] = []
    hunter._record_execution_submit_event = lambda **payload: events.append(payload)

    ok = hunter._submit_eth('0xdef', '11', 0.15, 'WETH', quantity=1, duration_hours=24)

    assert ok is False
    assert events[-1]['status'] == 'blocked'
    assert events[-1]['reason'] == 'eth_live_submit_disabled_until_governed_runtime'
