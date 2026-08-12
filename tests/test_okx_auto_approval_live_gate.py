from __future__ import annotations

import types

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient


TOKEN = "0x" + "1" * 40
SPENDER = "0x" + "2" * 40
NFT = "0x" + "3" * 40
OPERATOR = "0x" + "4" * 40
WALLET = "0x" + "5" * 40


class _FakeContractCall:
    def build_transaction(self, tx):
        pytest.fail(f"transaction build must not run while live approval is blocked: {tx}")


class _FakeFunctions:
    def approve(self, *_args):
        return _FakeContractCall()

    def setApprovalForAll(self, *_args):
        return _FakeContractCall()


class _FakeContract:
    functions = _FakeFunctions()


class _FakeEth:
    gas_price = 100

    def contract(self, **_kwargs):
        return _FakeContract()

    def send_raw_transaction(self, _raw):
        pytest.fail("broadcast must not run while live approval is blocked")


class _FakeWeb3:
    def __init__(self, _provider):
        self.eth = _FakeEth()

    @staticmethod
    def HTTPProvider(url):
        return url

    @staticmethod
    def to_checksum_address(value):
        return value


class _FakeAccountInstance:
    address = WALLET

    def sign_transaction(self, _tx):
        pytest.fail("signing must not run while live approval is blocked")


class _FakeAccount:
    @staticmethod
    def from_key(_private_key):
        return _FakeAccountInstance()


class _BlockedGovernor:
    instances = []

    def __init__(self, *, settings, **_kwargs):
        self.settings = settings
        self.gate_calls = []
        self.allocate_calls = []
        self.__class__.instances.append(self)

    def check_live_submit_allowed(self, **kwargs):
        self.gate_calls.append(kwargs)
        return "live arm required"

    def allocate_nonce(self, wallet, chain):
        self.allocate_calls.append((wallet, chain))
        pytest.fail("nonce allocation must not run while live approval is blocked")


def _bare_client():
    client = OKXAPIClient.__new__(OKXAPIClient)
    client.settings = object()
    client._primary_rpc = lambda _chain: "http://fake-rpc"
    return client


def _install_fakes(monkeypatch):
    import eth_account
    import web3
    import okx_nft_bot.execution_governor as governor_module

    _BlockedGovernor.instances.clear()
    monkeypatch.setattr(web3, "Web3", _FakeWeb3)
    monkeypatch.setattr(eth_account, "Account", _FakeAccount)
    monkeypatch.setattr(governor_module, "ExecutionGovernor", _BlockedGovernor)


def test_counterbid_package_installs_only_guarded_approval_methods():
    assert OKXAPIClient._auto_approve_erc20.__module__ == (
        "okx_nft_bot.counterbid.approval_safety"
    )
    assert OKXAPIClient._auto_approve_nft.__module__ == (
        "okx_nft_bot.counterbid.approval_safety"
    )

    # Safety-cancel primitives must remain the original implementations. R15 is
    # intentionally limited to permission-expanding approval transactions.
    assert OKXAPIClient._cancel_onchain_seaport.__module__ == (
        "okx_nft_bot.counterbid.okx_api"
    )
    assert OKXAPIClient._bump_counter_onchain.__module__ == (
        "okx_nft_bot.counterbid.okx_api"
    )


@pytest.mark.parametrize(
    ("method_name", "kwargs", "action_type", "subject"),
    [
        (
            "_auto_approve_erc20",
            {
                "token_address": TOKEN,
                "spender_address": SPENDER,
                "private_key": "0x" + "6" * 64,
                "chain_id": 56,
            },
            "LIVE_APPROVE_ERC20",
            TOKEN,
        ),
        (
            "_auto_approve_nft",
            {
                "nft_address": NFT,
                "operator_address": OPERATOR,
                "private_key": "0x" + "7" * 64,
                "chain_id": 56,
            },
            "LIVE_APPROVE_NFT",
            NFT,
        ),
    ],
)
def test_blocked_auto_approval_stops_before_nonce_and_broadcast(
    monkeypatch,
    method_name,
    kwargs,
    action_type,
    subject,
):
    _install_fakes(monkeypatch)
    client = _bare_client()

    with pytest.raises(RuntimeError, match="live arm required"):
        getattr(client, method_name)(**kwargs)

    assert len(_BlockedGovernor.instances) == 1
    governor = _BlockedGovernor.instances[0]
    assert governor.gate_calls == [
        {
            "action_type": action_type,
            "collection": subject.lower(),
            "chain": "bsc",
            "price_bnb": 0.0,
        }
    ]
    assert governor.allocate_calls == []
