"""One paid x402 search + settlement proof + receipt. Run from the repo root.

    .venv/bin/python scripts/daily_tick.py --query "..."

The receipt is written to disk the moment the balance drop proves settlement,
BEFORE settlement-tx location and proof formatting. A crash in the proof step
then costs a log-walk, not the paid response body (that loss happened once).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from bot import buy, chain  # noqa: E402

USDC = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"
TRANSFER = bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
QUOTED = 10000  # 0.01 USDC


def b(x) -> bytes:
    """HexBytes.hex() in this web3 returns NO 0x prefix — compare bytes, never strings."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    h = str(x)
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    args = ap.parse_args()

    w3 = chain.w3()
    wallet = Account.from_key(os.environ["AGENT_WALLET_KEY"]).address
    local_date = datetime.now().strftime("%Y-%m-%d")
    receipt_path = os.path.join(ROOT, "docs", "receipts", f"search-{local_date}.json")

    prior = [json.load(open(p)).get("query") for p in sorted(glob.glob(os.path.join(ROOT, "docs", "receipts", "search-*.json")))]
    if args.query in prior:
        print(f"WARNING: query already used by an earlier receipt: {args.query}")

    def balance() -> int:
        data = "0x70a08231" + wallet.lower()[2:].rjust(64, "0")
        raw = w3.eth.call({"to": Web3.to_checksum_address(USDC), "data": data})
        return int.from_bytes(raw, "big") if isinstance(raw, (bytes, bytearray)) else int(raw, 16)

    before = balance()
    if before < 50000:
        print(f"LOW_FUNDS: {before / 1e6:.6f} USDC at {wallet} — top up 0.20+ USDC")
        return 2

    t0 = time.time()
    resp = buy.pay_and_call(buy.BASE + "/api/search", {"q": args.query})
    elapsed = round(time.time() - t0, 1)
    print(json.dumps({"api_ok": True, "count": resp.get("count"), "elapsed_s": elapsed}))

    after, deadline = before, time.time() + 45
    while time.time() < deadline:
        after = balance()
        if before - after >= QUOTED:
            break
        time.sleep(3)
    spent = before - after
    if spent < QUOTED:
        print(f"NOT_SETTLED: balance {before} -> {after}, no receipt written")
        return 3

    receipt = {
        "date": local_date, "network": "eip155:42220", "network_name": "Celo",
        "payer": wallet, "query": args.query,
        "count": resp.get("count"), "results_count": len(resp.get("results") or []),
        "usdc_before_atomic": before, "usdc_after_atomic": after, "usdc_spent_atomic": spent,
        "usdc_before": before / 1e6, "usdc_after": after / 1e6, "usdc_spent": spent / 1e6,
        "paid_usdc_atomic": QUOTED, "response": resp,
        "settlement_tx": None, "settlement_block": None, "settlement_status": None,
        "attempts": 1, "elapsed_s": elapsed, "payee": None,
    }
    with open(receipt_path, "w") as f:  # body is safe on disk before any proof work
        json.dump(receipt, f, indent=1)
    print("RECEIPT_SEEDED", receipt_path)

    padded = bytes.fromhex(wallet.lower()[2:].rjust(64, "0"))
    post_head = w3.eth.block_number
    match, hi = None, post_head + 2
    for _ in range(14):
        lo = max(0, hi - 100)
        try:
            logs = w3.eth.get_logs({"address": Web3.to_checksum_address(USDC), "fromBlock": lo,
                                    "toBlock": hi, "topics": [TRANSFER, padded]})
        except Exception as e:  # noqa: BLE001 — Forno rejects wide ranges; just walk narrower
            print("log window failed", lo, hi, str(e)[:100])
            logs = []
        for lg in logs:
            if b(lg["data"]) and int.from_bytes(b(lg["data"]), "big") == spent:
                match = lg
        if match:
            break
        hi = lo - 1
        if hi <= 0:
            break
    if not match:
        print("NO_SETTLEMENT_LOG — receipt keeps the settlement-less seed; investigate before committing")
        return 4

    txh = "0x" + b(match["transactionHash"]).hex()
    rcpt = w3.eth.get_transaction_receipt(txh)
    ok_logs = [lg for lg in rcpt["logs"] if b(lg["topics"][0]) == TRANSFER
               and b(lg["topics"][1]) == padded
               and b(lg["data"]) and int.from_bytes(b(lg["data"]), "big") == spent]
    ok = rcpt["status"] == 1 and bool(ok_logs)
    print(json.dumps({"settlement_tx": txh, "block": rcpt["blockNumber"], "status": rcpt["status"],
                      "transfer_log_ok": bool(ok_logs), "payee": "0x" + b(match["topics"][2])[-20:].hex()}))

    receipt.update({
        "settlement_tx": txh, "settlement_block": rcpt["blockNumber"],
        "settlement_status": hex(rcpt["status"]),
        "payee": "0x" + b(match["topics"][2])[-20:].hex(),
    })
    with open(receipt_path, "w") as f:
        json.dump(receipt, f, indent=1)
    print("RECEIPT_FINAL" if ok else "RECEIPT_UNPROVEN", receipt_path)
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
