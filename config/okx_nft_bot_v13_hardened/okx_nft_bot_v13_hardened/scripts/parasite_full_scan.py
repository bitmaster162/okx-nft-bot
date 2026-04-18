#!/usr/bin/env python3
"""
Full Parasite Scan — All 44 addresses across multiple sources.

Sources:
  1. OKX priapi — active offers on OKX NFT Marketplace (BSC + ETH)
  2. OKX Web3 aggregator — cross-marketplace collection offers
  3. BscScan/Etherscan — on-chain Seaport approvals & activity

Output: JSON report + human-readable summary + Telegram alert.

Usage:
    python scripts/parasite_full_scan.py
    python scripts/parasite_full_scan.py --chains bsc
    python scripts/parasite_full_scan.py --wallets 0x28c974c1...,0x03566d77...
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

# ── All 44 parasite wallets ──
PARASITE_WALLETS = [
    "0xeb66ee23e59fc660b7fb950bc7b21c6d50d8ec62",
    "0x8ac0fc25d7880bb2ae9079f81da6483c6692374b",
    "0xf1771cf8831393422189330a79dd896223c357a4",
    "0x38ec6d8d21b639e300a609dd7e538c7fbf7a8181",
    "0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7",
    "0x692e3716b9fceb26855693b4c6cf0a83c869947b",
    "0xef52555e4489b52907ab24ef9d6d6f1886b26090",
    "0x824d8f4bf6e4e428e9d18cc40c86587e15fdfd21",
    "0xd5e7d7234bc1b473b981ce90b751bb734b33f078",
    "0xa025c7cc8f49e1bc1fda8e6bcb1aa5fc9ec29bc2",
    "0x59d4641297134d711b3bb2e1e2ec199a187a3bab",
    "0xe7461ac89a298c4b76a70ceaabf7fe58805f996c",
    "0xa58e0e571ea7f3e4203b18f689b8eb294821d1f8",
    "0x5bc4d5a099432a24a88c94f6720d07abb2d27890",
    "0xcbf2f4664ecaa9d79ddc3bc1636aa1b788d09933",
    "0x710023662f3e43e0712e679c121440fc8365b519",
    "0x964813b67e869abd49e7b9ed4e27ec808385fd27",
    "0xb34a74bc03a5767c1a2cd9b7466cb11390d6ed3a",
    "0x0e6a44afb321075139b98b2eace33598e577bde2",
    "0x2ee38382bcf2b2ac406cab64a215683f12ec6dff",
    "0x47e75cfb1b594eea586ed67cfb868b70354be576",
    "0xb9c3c10e8efc6a894d6d86c188f014d44bac63cc",
    "0x7344b7c345b83472d04d78bb351b00ffb8fb3d5c",
    "0x01c33c17f829b08576d6b61cf057f16f29732c42",
    "0x8dbbbf494a5d27078d7371e27819458fdad19bc8",
    "0xdcab7053462fe8f46b6f046d465cd01cdbc0efe9",
    "0x342a6981e25e4d812f6c102cd9568835e869a226",
    "0x16bbe4f1b7bf38290e04cbc9b4c19cc31058fa26",
    "0xd8f24f5f0382e197c1e87ad82b357209383470cf",
    "0xec3affd51db323e9563b4645281451d2739ab488",
    "0x2c7fdcdc0936be340db8e04a031f6259aa2c7b2c",
    "0x1194cbebee18d7c8c3440d58161e4e03e9011bf4",
    "0x14664b428030d4ee8189f16214198869ceb2fd96",
    "0xa463a71a942ec87feed2f903dcaa55057c92e7ea",
    "0xd2ab77a10cab66433c167e9aa4cc3178fe133def",
    "0xf6310f92b6ac4a16448687f493ae1762c7603115",
    "0xc4284012812998c260f050dea5c767872648c567",
    "0x9b435397f186bbcd6cb1e8d0681acda4d2c97a3d",
    "0xa70c9192e1126f48298d3e274d1e4598a5252bce",
    "0xde88170d713695224dc0a3b5b22906601ca40400",
    "0x64d5103e918e0ddf22412a2cb232a4613682d8c2",
    "0x28c974c18c6553b445c4b77f35d83f3499acf58f",  # Main parasite
    "0xa845c74344fc9405b1fcf712f04668979573c1bf",
    "0x03566d779d1d4e47d042a32a45da594796e60365",  # Master controller
]

OUR_WALLET = "0xeabe45e942451aa77c113a6397d673d491f22095"

# ── OKX priapi config ──
OKX_PRIAPI_BASE = "https://web3.okx.com/priapi/v5/nft"
OKX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://web3.okx.com",
    "Referer": "https://web3.okx.com/",
}

CHAIN_IDS = {"bsc": "56", "eth": "1", "polygon": "137", "arbitrum": "42161", "base": "8453"}


def _http_get(url: str, headers: dict = None, timeout: int = 15) -> dict | None:
    """Simple HTTP GET returning JSON or None on error."""
    hdrs = headers or {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"    ⏳ Rate limited, waiting 3s...")
            time.sleep(3)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                return json.loads(resp.read())
            except Exception:
                return None
        return None
    except Exception as e:
        print(f"    ❌ {e}")
        return None


# ══════════════════════════════════════════════════════════════
# SOURCE 1: OKX priapi — wallet offers (token + collection)
# ══════════════════════════════════════════════════════════════

def scan_okx_wallet_offers(wallet: str, chain_id: str) -> list[dict]:
    """Fetch all active offers FROM a specific wallet on OKX."""
    all_offers = []

    # Token offers
    for endpoint in ["ec/offer/list", "ec/collection-offer/list"]:
        page = 1
        while page <= 10:
            url = (f"{OKX_PRIAPI_BASE}/{endpoint}?"
                   f"makerAddress={wallet}&chainId={chain_id}"
                   f"&status=active&limit=50&page={page}")
            data = _http_get(url, OKX_HEADERS)
            if not data:
                break

            items = data.get("data", {})
            if isinstance(items, dict):
                items = items.get("offers", items.get("data", items.get("list", [])))
            if not items:
                break

            for item in items:
                maker = (item.get("maker") or item.get("makerAddress") or "").lower()
                if maker != wallet.lower():
                    continue
                all_offers.append({
                    "source": "okx",
                    "type": "token_offer" if "collection-offer" not in endpoint else "collection_offer",
                    "order_id": item.get("orderId") or item.get("orderHash") or "",
                    "maker": maker,
                    "collection": (item.get("collectionAddress") or item.get("nftAddress") or "").lower(),
                    "collection_name": item.get("collectionName") or item.get("projectName") or "",
                    "token_id": item.get("tokenId") or "",
                    "price": item.get("price") or "0",
                    "currency": item.get("currencyName") or item.get("currency") or "",
                    "status": item.get("status") or "active",
                })

            if len(items) < 50:
                break
            page += 1
            time.sleep(0.3)

    return all_offers


def scan_okx_collection_top_offers(wallet: str, chain_id: str) -> list[dict]:
    """Check if wallet has top offers on ANY collection via priapi offer/list.

    Scans public offer list and filters by maker. Catches offers that
    wallet-level queries might miss.
    """
    # This is slower but catches cross-collection offers
    # We scan top offers for known collections where parasites operate
    return []  # Will be populated in Phase 2 integration


# ══════════════════════════════════════════════════════════════
# SOURCE 2: OKX Web3 Marketplace API — broader marketplace data
# ══════════════════════════════════════════════════════════════

def scan_okx_marketplace_activity(wallet: str, chain_id: str) -> list[dict]:
    """Fetch recent marketplace activity (buys/sells/offers) for wallet."""
    activities = []

    url = (f"{OKX_PRIAPI_BASE}/ec/activity/list?"
           f"address={wallet}&chainId={chain_id}"
           f"&type=offer&limit=50")
    data = _http_get(url, OKX_HEADERS)
    if not data:
        return activities

    items = data.get("data", {})
    if isinstance(items, dict):
        items = items.get("list", items.get("data", []))

    for item in (items or []):
        activities.append({
            "source": "okx_activity",
            "type": item.get("type") or "offer",
            "collection": (item.get("collectionAddress") or "").lower(),
            "collection_name": item.get("collectionName") or "",
            "price": item.get("price") or "0",
            "currency": item.get("currencyName") or "",
            "timestamp": item.get("timestamp") or item.get("createTime") or "",
        })

    return activities


# ══════════════════════════════════════════════════════════════
# SOURCE 3: BscScan/Etherscan — on-chain token approvals
# Checks if wallet has approved Seaport/Element/other marketplace
# contracts to spend their tokens (WBNB, USDT, etc.)
# ══════════════════════════════════════════════════════════════

# Known marketplace contracts on BSC
MARKETPLACE_CONTRACTS = {
    "bsc": {
        "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "Seaport 1.5",
        "0x00000000000001ad428e4906ae43d8f9852d0dd6": "Seaport 1.6",
        "0xb4a437caE9a15CDe291780c65A3cF8bBe7252FCc": "Element Exchange",
        "0x20F780A973856B93f63670377900C1d2a50a77c4": "Element Exchange V2",
    },
    "eth": {
        "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "Seaport 1.5",
        "0x00000000000001ad428e4906ae43d8f9852d0dd6": "Seaport 1.6",
        "0x000000000000Ad05Ccc4F10045630fb830B95127": "Blur",
    }
}

# Common currency tokens
TOKEN_CONTRACTS = {
    "bsc": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
        "0x55d398326f99059ff775485246999027b3197955": "USDT",
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
        "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
    },
    "eth": {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    }
}

SCAN_APIS = {
    "bsc": "https://api.bscscan.com/api",
    "eth": "https://api.etherscan.io/api",
}


def scan_token_approvals(wallet: str, chain: str, api_key: str = "") -> list[dict]:
    """Check on-chain ERC20 approvals to marketplace contracts.

    If wallet approved WBNB/USDT to Seaport = they can place offers.
    """
    approvals = []
    scan_api = SCAN_APIS.get(chain)
    if not scan_api:
        return approvals

    tokens = TOKEN_CONTRACTS.get(chain, {})
    marketplaces = MARKETPLACE_CONTRACTS.get(chain, {})

    for token_addr, token_name in tokens.items():
        # Get approval events (ERC20 Approval topic)
        # Topic0 = Approval(address,address,uint256)
        approval_topic = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
        # Topic1 = owner (padded wallet)
        topic1 = "0x" + wallet.lower().replace("0x", "").zfill(64)

        params = {
            "module": "logs",
            "action": "getLogs",
            "address": token_addr,
            "topic0": approval_topic,
            "topic1": topic1,
            "fromBlock": "0",
            "toBlock": "latest",
            "page": "1",
            "offset": "100",
        }
        if api_key:
            params["apikey"] = api_key

        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{scan_api}?{query}"

        data = _http_get(url)
        if not data or data.get("status") != "1":
            continue

        results = data.get("result", [])
        for log_entry in results:
            topics = log_entry.get("topics", [])
            if len(topics) >= 3:
                # Topic2 = spender (padded address)
                spender_raw = topics[2]
                spender = "0x" + spender_raw[-40:].lower()

                marketplace_name = marketplaces.get(spender.lower(), "")
                if marketplace_name:
                    # Parse approval amount from data
                    raw_data = log_entry.get("data", "0x0")
                    try:
                        amount_wei = int(raw_data, 16)
                        # Max uint256 = unlimited approval
                        is_unlimited = amount_wei > 10**50
                    except (ValueError, TypeError):
                        amount_wei = 0
                        is_unlimited = False

                    block_hex = log_entry.get("blockNumber", "0x0")
                    try:
                        block_num = int(block_hex, 16)
                    except (ValueError, TypeError):
                        block_num = 0

                    approvals.append({
                        "source": "on_chain",
                        "token": token_name,
                        "token_address": token_addr,
                        "spender": spender,
                        "marketplace": marketplace_name,
                        "amount_unlimited": is_unlimited,
                        "block": block_num,
                        "tx_hash": log_entry.get("transactionHash", ""),
                    })

        time.sleep(0.25)  # BscScan rate limit

    return approvals


def scan_wallet_balance(wallet: str, chain: str) -> dict:
    """Check currency balances via RPC."""
    rpc_urls = {
        "bsc": "https://bsc-dataseed.binance.org/",
        "eth": "https://eth.llamarpc.com",
    }
    rpc = rpc_urls.get(chain)
    if not rpc:
        return {}

    tokens = TOKEN_CONTRACTS.get(chain, {})
    balances = {}

    for token_addr, token_name in tokens.items():
        addr_padded = wallet.lower().replace('0x', '').zfill(64)
        call_data = '0x70a08231' + addr_padded
        payload = json.dumps({
            'jsonrpc': '2.0', 'method': 'eth_call',
            'params': [{'to': token_addr, 'data': call_data}, 'latest'], 'id': 1,
        }).encode()

        req = urllib.request.Request(rpc, data=payload,
                                      headers={'Content-Type': 'application/json'})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            raw = int(resp.get('result', '0x0'), 16)
            # USDT/USDC on ETH = 6 decimals, others = 18
            decimals = 6 if token_name in ("USDT", "USDC") and chain == "eth" else 18
            balance = raw / (10 ** decimals)
            if balance > 0.0001:
                balances[token_name] = round(balance, 6)
        except Exception:
            pass

    # Native balance (BNB/ETH)
    try:
        payload = json.dumps({
            'jsonrpc': '2.0', 'method': 'eth_getBalance',
            'params': [wallet, 'latest'], 'id': 1,
        }).encode()
        req = urllib.request.Request(rpc, data=payload,
                                      headers={'Content-Type': 'application/json'})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        raw = int(resp.get('result', '0x0'), 16)
        native = raw / (10 ** 18)
        native_name = "BNB" if chain == "bsc" else "ETH"
        if native > 0.0001:
            balances[native_name] = round(native, 6)
    except Exception:
        pass

    return balances


# ══════════════════════════════════════════════════════════════
# MAIN SCANNER
# ══════════════════════════════════════════════════════════════

def run_full_scan(wallets: list[str], chains: list[str],
                  bscscan_key: str = "", etherscan_key: str = "") -> dict:
    """Run full multi-source scan."""

    report = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "wallets_scanned": len(wallets),
        "chains": chains,
        "our_wallet": OUR_WALLET,
        "sources": ["okx_priapi", "okx_activity", "on_chain_approvals", "balances"],
        "summary": {
            "total_active_offers": 0,
            "total_marketplace_approvals": 0,
            "wallets_with_offers": 0,
            "wallets_with_approvals": 0,
            "wallets_with_balance": 0,
            "collections_targeted": set(),
            "marketplaces_detected": set(),
        },
        "wallets": {},
    }

    total = len(wallets)

    for i, wallet in enumerate(wallets):
        if wallet.lower() == OUR_WALLET.lower():
            print(f"  ⚠️ [{i+1}/{total}] SKIPPING OUR WALLET")
            continue

        short = f"{wallet[:10]}...{wallet[-4:]}"
        print(f"\n  [{i+1}/{total}] 🕷 {short}")

        wallet_data = {
            "address": wallet,
            "offers": [],
            "activity": [],
            "approvals": [],
            "balances": {},
            "summary": {
                "total_offers": 0,
                "has_marketplace_approval": False,
                "has_balance": False,
                "chains_active": [],
                "marketplaces": set(),
                "collections": set(),
            }
        }

        for chain in chains:
            chain_id = CHAIN_IDS.get(chain, "56")
            print(f"    📡 {chain.upper()}: ", end="")
            sys.stdout.flush()

            # Source 1: OKX offers
            offers = scan_okx_wallet_offers(wallet, chain_id)
            if offers:
                wallet_data["offers"].extend(offers)
                wallet_data["summary"]["total_offers"] += len(offers)
                wallet_data["summary"]["chains_active"].append(chain)
                for o in offers:
                    if o["collection"]:
                        wallet_data["summary"]["collections"].add(o["collection"])
                        report["summary"]["collections_targeted"].add(o["collection"])
                    wallet_data["summary"]["marketplaces"].add("okx")
                    report["summary"]["marketplaces_detected"].add("okx")
                print(f"OKX={len(offers)} offers ", end="")
            else:
                print(f"OKX=0 ", end="")

            # Source 2: OKX activity
            activity = scan_okx_marketplace_activity(wallet, chain_id)
            if activity:
                wallet_data["activity"].extend(activity)
                print(f"activity={len(activity)} ", end="")
            else:
                print(f"activity=0 ", end="")

            # Source 3: On-chain approvals (only BSC and ETH)
            api_key = bscscan_key if chain == "bsc" else etherscan_key if chain == "eth" else ""
            if chain in SCAN_APIS:
                approvals = scan_token_approvals(wallet, chain, api_key)
                if approvals:
                    wallet_data["approvals"].extend(approvals)
                    wallet_data["summary"]["has_marketplace_approval"] = True
                    for a in approvals:
                        wallet_data["summary"]["marketplaces"].add(a["marketplace"])
                        report["summary"]["marketplaces_detected"].add(a["marketplace"])
                    print(f"approvals={len(approvals)} ", end="")
                else:
                    print(f"approvals=0 ", end="")

            # Source 4: Balance check
            balances = scan_wallet_balance(wallet, chain)
            if balances:
                wallet_data["balances"][chain] = balances
                wallet_data["summary"]["has_balance"] = True
                bal_str = " ".join(f"{v}{k}" for k, v in balances.items())
                print(f"balance=[{bal_str}]", end="")
            else:
                print(f"balance=0", end="")

            print()  # newline
            time.sleep(0.5)

        # Finalize wallet data
        wallet_data["summary"]["marketplaces"] = list(wallet_data["summary"]["marketplaces"])
        wallet_data["summary"]["collections"] = list(wallet_data["summary"]["collections"])
        report["wallets"][wallet] = wallet_data

        # Update global summary
        if wallet_data["summary"]["total_offers"] > 0:
            report["summary"]["wallets_with_offers"] += 1
            report["summary"]["total_active_offers"] += wallet_data["summary"]["total_offers"]
        if wallet_data["summary"]["has_marketplace_approval"]:
            report["summary"]["wallets_with_approvals"] += 1
        if wallet_data["summary"]["has_balance"]:
            report["summary"]["wallets_with_balance"] += 1

    # Convert sets to lists for JSON
    report["summary"]["collections_targeted"] = list(report["summary"]["collections_targeted"])
    report["summary"]["marketplaces_detected"] = list(report["summary"]["marketplaces_detected"])

    return report


def print_summary(report: dict):
    """Print human-readable summary."""
    s = report["summary"]

    print("\n" + "=" * 70)
    print("  FULL PARASITE SCAN — SUMMARY")
    print("=" * 70)
    print(f"  Scan time:  {report['scan_time']}")
    print(f"  Wallets:    {report['wallets_scanned']} scanned on {', '.join(report['chains'])}")
    print()
    print(f"  🕷 Active offers:              {s['total_active_offers']}")
    print(f"  👛 Wallets with offers:        {s['wallets_with_offers']}/{report['wallets_scanned']}")
    print(f"  🔑 Wallets with approvals:     {s['wallets_with_approvals']}/{report['wallets_scanned']}")
    print(f"  💰 Wallets with balance:       {s['wallets_with_balance']}/{report['wallets_scanned']}")
    print(f"  🏪 Marketplaces detected:      {', '.join(s['marketplaces_detected']) or 'none'}")
    print(f"  📦 Collections targeted:       {len(s['collections_targeted'])}")

    # Top parasites
    active = []
    for addr, w in report["wallets"].items():
        if w["summary"]["total_offers"] > 0 or w["summary"]["has_marketplace_approval"]:
            active.append(w)

    if active:
        active.sort(key=lambda w: w["summary"]["total_offers"], reverse=True)
        print(f"\n{'─' * 70}")
        print("  ACTIVE PARASITES:")
        print(f"{'─' * 70}")
        for i, w in enumerate(active[:20], 1):
            addr = w["address"]
            s = w["summary"]
            chains = ", ".join(s["chains_active"]) if s["chains_active"] else "—"
            markets = ", ".join(s["marketplaces"]) if s["marketplaces"] else "—"
            bals = []
            for chain, b in w["balances"].items():
                bals.extend(f"{v:.4f} {k}" for k, v in b.items())
            bal_str = " | ".join(bals) if bals else "—"
            print(f"  {i:2d}. {addr[:14]}...{addr[-4:]} | "
                  f"{s['total_offers']:3d} offers | {len(s['collections'])} colls | "
                  f"{markets} | {chains}")
            if bals:
                print(f"      💰 {bal_str}")

    # Wallets with approvals but no active offers (dormant but ready)
    dormant = [w for addr, w in report["wallets"].items()
               if w["summary"]["has_marketplace_approval"] and w["summary"]["total_offers"] == 0]
    if dormant:
        print(f"\n{'─' * 70}")
        print(f"  DORMANT (approved but no offers): {len(dormant)} wallets")
        print(f"{'─' * 70}")
        for w in dormant[:10]:
            addr = w["address"]
            markets = set()
            for a in w["approvals"]:
                markets.add(f"{a['marketplace']}({a['token']})")
            print(f"  {addr[:14]}...{addr[-4:]} | approvals: {', '.join(markets)}")

    # Collections being targeted
    if s["collections_targeted"]:
        # Group offers by collection
        coll_map = defaultdict(list)
        for addr, w in report["wallets"].items():
            for o in w["offers"]:
                if o["collection"]:
                    coll_map[o["collection"]].append(o)

        top_colls = sorted(coll_map.items(), key=lambda x: len(x[1]), reverse=True)
        print(f"\n{'─' * 70}")
        print("  TOP TARGETED COLLECTIONS:")
        print(f"{'─' * 70}")
        for i, (coll, offers) in enumerate(top_colls[:20], 1):
            name = offers[0].get("collection_name") or coll[:14]
            parasites = len({o["maker"] for o in offers})
            print(f"  {i:2d}. {name[:35]:35s} | {len(offers)} offers from {parasites} parasites")

    print(f"\n{'=' * 70}")


def send_telegram_report(report: dict):
    """Send summary to Telegram."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return

    s = report["summary"]
    msg = (
        f"🕷 <b>PARASITE FULL SCAN</b>\n\n"
        f"Wallets: {report['wallets_scanned']}\n"
        f"Chains: {', '.join(report['chains'])}\n\n"
        f"📊 Active offers: {s['total_active_offers']}\n"
        f"👛 With offers: {s['wallets_with_offers']}/{report['wallets_scanned']}\n"
        f"🔑 With approvals: {s['wallets_with_approvals']}/{report['wallets_scanned']}\n"
        f"💰 With balance: {s['wallets_with_balance']}/{report['wallets_scanned']}\n"
        f"🏪 Marketplaces: {', '.join(s['marketplaces_detected']) or 'none'}\n"
        f"📦 Collections: {len(s['collections_targeted'])}\n"
    )

    payload = json.dumps({
        "chat_id": chat_id, "text": msg, "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        print("📱 Telegram report sent!")
    except Exception as e:
        print(f"📱 Telegram send failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Full Parasite Scan")
    parser.add_argument("--chains", default="bsc,eth",
                        help="Comma-separated chains (default: bsc,eth)")
    parser.add_argument("--wallets", default="",
                        help="Comma-separated wallets (default: all 44)")
    parser.add_argument("--output", default="data/parasite_scan_report.json",
                        help="Output JSON path")
    parser.add_argument("--bscscan-key", default=os.getenv("BSCSCAN_API_KEY", ""),
                        help="BscScan API key")
    parser.add_argument("--etherscan-key", default=os.getenv("ETHERSCAN_API_KEY", ""),
                        help="Etherscan API key")
    parser.add_argument("--telegram", action="store_true",
                        help="Send summary to Telegram")
    args = parser.parse_args()

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    wallets = PARASITE_WALLETS
    if args.wallets:
        wallets = [w.strip().lower() for w in args.wallets.split(",") if w.strip()]

    print("🕷 PARASITE KILLER — Full Multi-Source Scan")
    print(f"   Wallets: {len(wallets)}")
    print(f"   Chains:  {', '.join(chains)}")
    print(f"   Sources: OKX priapi + OKX activity + on-chain approvals + balances")
    est_min = len(wallets) * len(chains) * 2.5 / 60
    print(f"   Estimated time: {est_min:.1f} min")
    print()

    report = run_full_scan(wallets, chains, args.bscscan_key, args.etherscan_key)

    # Save
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n💾 Report saved to {args.output}")

    print_summary(report)

    if args.telegram:
        send_telegram_report(report)


if __name__ == "__main__":
    main()
