"""Services the agent buys per invoice — paid calls into systems it does not own.

An invoice agent that only parses text and watches a chain is a script. The
valuable part is what it has to *buy* to let money move: we do not run a
sanctions list, so the agent rents one, per invoice, and the receipt proves it
paid. Roughly $0.009 per invoice across three calls:

- `fx-rate`         ($0.002) independent cross-check of the rate we priced with
- `sanctions-wallet` ($0.002) screen the payout address
- `sanctions-name`  ($0.005) screen the counterparty name

An exact sanctions hit blocks settlement of that invoice. That is the whole point: the
purchase is not a demo of paying, it is the gate.

Rules that keep this honest:
- The raw response is written to disk BEFORE it is parsed. A lost paid body is
  unrecoverable, and the wallet-keyed receipt endpoints refuse Celo payments.
- Every paid run carries an Idempotency-Key so a retry replays instead of
  charging twice.
- Settlement is verified on-chain (receipt status + a USDC Transfer from our
  payer) instead of trusting the PAYMENT-RESPONSE header, which is just the
  seller's own claim about itself.
- A failed purchase never blocks a mint and never invents a result: the invoice
  records the failure and the page says so.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from . import buy, chain

BASE = "https://agent402.tools"
RECEIPT_DIR = Path(os.environ.get("SERVICE_RECEIPT_DIR", "docs/receipts"))
USDC_CELO = "0xceba9300f2b948710d2653dD7B07f33A8B32118C".lower()
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# slug -> paid route. Prices are the list price read from /api/pricing; the
# authoritative amount is whatever the 402 offer asks, so never trust these
# numbers for a spend decision (buy.spend_gate reads the offer).
SERVICES = {
    "fx-rate": {"route": "/api/fx-rate", "price_usd": 0.002,
                "why": "cross-check the FX rate this invoice was priced with"},
    "sanctions-wallet": {"route": "/api/sanctions/wallet", "price_usd": 0.002,
                         "why": "screen the payout address against sanctions lists"},
    "sanctions-name": {"route": "/api/sanctions/name", "price_usd": 0.005,
                       "why": "screen the counterparty name against sanctions lists"},
}

CLEAN_VERDICT = "no_match_on_lists_checked"


def _receipt_path(invoice_id: str, slug: str) -> Path:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return RECEIPT_DIR / f"buy-{invoice_id}-{slug}-{stamp}.json"


def verify_settlement(tx_hash: str, payer: str, *, web3=None) -> dict:
    """Prove the payment landed: tx succeeded and USDC left our payer.

    The PAYMENT-RESPONSE header is the seller describing its own settlement. This
    reads the chain instead.
    """
    try:
        client = web3 or chain.w3()
        rcpt = client.eth.get_transaction_receipt(tx_hash)
        if rcpt is None:
            return {"verified": False, "reason": "tx not found"}
        moved = 0
        for lg in rcpt["logs"]:
            topics = [t.hex() if isinstance(t, bytes) else t for t in lg["topics"]]
            if not topics or ("0x" + topics[0].lstrip("0x")).lower() != TRANSFER_TOPIC:
                continue
            if len(topics) < 3:
                continue
            sender = "0x" + topics[1][-40:]
            if sender.lower() != payer.lower():
                continue
            raw = lg["data"].hex() if isinstance(lg["data"], bytes) else lg["data"]
            moved += int(raw, 16)
        return {"verified": rcpt["status"] == 1 and moved > 0,
                "atomic": moved, "block": rcpt["blockNumber"],
                "reason": "ok" if moved > 0 else "no USDC transfer from payer"}
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "reason": f"{type(e).__name__}: {e}"}


def purchase(slug: str, params: dict, invoice_id: str, *, key: str | None = None,
             session=None, web3=None, log=print) -> dict:
    """One paid call. Returns a receipt dict; never raises on a paid-call failure."""
    service = SERVICES[slug]
    url = BASE + service["route"]
    receipt = {"slug": slug, "why": service["why"], "url": url, "params": params,
               "price_usd_quoted": service["price_usd"], "at": int(time.time())}
    idem = f"{invoice_id}-{slug}-{receipt['at']}"
    try:
        if session is None:
            from dotenv import dotenv_values
            k = key or dotenv_values(".env").get("AGENT_WALLET_KEY")
            session = buy.paid_session(k, budget_url=url)
        r = session.get(url, params=params, timeout=150,
                        headers={"Idempotency-Key": idem})
        # Capture before parsing: a paid body cannot be re-fetched for free.
        raw = {"status": r.status_code, "headers": dict(r.headers), "text": r.text}
        path = _receipt_path(invoice_id, slug)
        path.write_text(json.dumps(raw, indent=2))
        receipt["receipt_file"] = str(path)
        receipt["http_status"] = r.status_code

        pay_header = r.headers.get("PAYMENT-RESPONSE")
        if pay_header:
            try:
                pay = json.loads(base64.b64decode(pay_header))
                receipt["payment"] = pay
                receipt["tx"] = pay.get("transaction")
                if pay.get("transaction") and pay.get("payer"):
                    receipt["onchain"] = verify_settlement(pay["transaction"],
                                                          pay["payer"], web3=web3)
            except (ValueError, TypeError):
                pass

        if r.status_code != 200:
            receipt.update({"ok": False, "error": f"HTTP {r.status_code}"})
            return receipt
        body = json.loads(r.text)
        receipt["ok"] = True
        receipt["result"] = _summarize(slug, body)
        return receipt
    except Exception as e:  # noqa: BLE001 — a purchase failure must not break the invoice
        receipt.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        log(f"[services] {slug} failed for {invoice_id}: {receipt['error']}")
        return receipt


def _summarize(slug: str, body: dict) -> dict:
    """Keep the decision, drop the bulk."""
    if slug.startswith("sanctions"):
        matches = body.get("matches") or []
        return {"verdict": body.get("verdict"),
                "query": body.get("query"),
                "exactCount": body.get("exactCount"),
                # The signal that separates a real hit from substring noise:
                # allTokensCount > 0 means EVERY token of the queried name appears in
                # one listed entry. containsCount just means a token appeared somewhere
                # ('Anna' hits 'DI ANNABA, Yacine'; 'Bao' hits 'Bilbao').
                "allTokensCount": body.get("allTokensCount"),
                "containsCount": body.get("containsCount"),
                "top": [{"name": m.get("name"), "id": m.get("id"),
                         "matchType": m.get("matchType")} for m in matches[:3]],
                "matches": len(matches),
                "lists": [x.get("list") for x in (body.get("listsChecked") or [])],
                "clean": is_clean(body)}
    if slug == "fx-rate":
        return {"rate": body.get("rate") or body.get("result") or body,
                "clean": None}
    return {"clean": None}



def is_clean(body: dict) -> bool:
    """A sanctions screen is clean only on an explicit no-match verdict."""
    if body.get("verdict") != CLEAN_VERDICT:
        return False
    return int(body.get("exactCount") or 0) == 0


# A screen returns three different things behind one word, "verdict". Treating them
# the same refused every invoice whose counterparty had a common name.
CLEAN, HOLD, BLOCK = "clean", "hold", "block"


def screen_verdict(slug: str, result: dict) -> tuple[str, str]:
    """What one paid screen actually licenses: refusal, a human decision, or clear.

    The vendor's `verdict` word is not a decision by itself. It also returns
    `exactCount` (exact string equality — effectively always 0), `allTokensCount`
    (every token of the queried name appears in ONE listed entry) and `containsCount`
    (some token appears somewhere). Verified live against OFAC SDN:

        'Viktor Bout' → match, allTokens=1, contains=0 → "BOUT, Viktor Anatolijevitch"
                        (SDN 8279) — a real hit
        'Anna'  → match, allTokens=0, contains=34 → "DI ANNABA, Yacine" — substring noise
        'Bao'   → match, allTokens=0, contains=8  → "Bilbao", "LIU, Baoxia" — noise
        'Jack'  → match, allTokens=0, contains=1  → "DERMAN GUZMAN, Jack Peter" — noise
        'Jack Miller' → no_match_on_lists_checked                      — clear

    So an all-token match on a name of two or more tokens blocks; substring noise and
    any single-token query (one token cannot identify a person) open a hold a human
    adjudicates, quoting the entry and its SDN id so it can be checked against the
    list. A wallet address is exact by construction, so it has no fuzzy tier.
    """
    result = result or {}
    verdict = str(result.get("verdict") or "")
    exact = int(result.get("exactCount") or 0)
    if slug.startswith("sanctions-wallet"):
        if result.get("clean"):
            return CLEAN, ""
        return BLOCK, f"sanctions-wallet is on a list: verdict={verdict}"
    if result.get("clean"):
        return CLEAN, ""
    query = str(result.get("query") or "").strip()
    tokens = len(query.split())
    all_tokens = int(result.get("allTokensCount") or 0)
    top = result.get("top") or []
    named = ", ".join(f"{(m.get('name') or '?')} (SDN {m.get('id') or '?'})" for m in top[:2])
    listed = ", ".join(result.get("lists") or []) or "the list checked"
    if exact > 0:
        return BLOCK, f"sanctions-name exact hit on {listed}: {named or query!r}"
    if all_tokens > 0 and tokens >= 2:
        return BLOCK, (f"sanctions-name match on every token of {query!r} in {listed}: "
                       f"{named} — check the entry against the list before acting")
    if all_tokens > 0:
        return HOLD, (f"single-token name {query!r} matches a listed entry ({named} in "
                      f"{listed}) but one token cannot identify a person — a full name "
                      f"needs to be screened")
    return HOLD, (f"sanctions-name found {result.get('containsCount') or 0} entry/entries "
                  f"containing part of {query!r} on {listed} and no all-token match"
                  + (f" (top: {named})" if named else ""))




def plan_for(invoice: dict) -> list[tuple[str, dict]]:
    """Which services this invoice needs bought, in order."""
    out = []
    currency = str(invoice.get("currency") or "USD").upper()
    # An in-kind invoice (500 BRL settling as 500 wBRL) involves no conversion,
    # so buying an FX cross-check for it would be paying for a number nobody uses.
    in_kind = bool(invoice.get("settle_asset"))
    if currency not in ("USD", "USAT", "USDC", "USDT") and not in_kind:
        out.append(("fx-rate", {"from": "USD", "to": currency, "amount": 1}))
    payout = invoice.get("payout_address")
    if payout:
        out.append(("sanctions-wallet", {"address": payout}))
    name = (invoice.get("recipient") or "").strip()
    if name and not name.startswith("@") and len(name) >= 3:
        out.append(("sanctions-name", {"name": name, "limit": 5}))
    return out


def enrich(invoice: dict, *, key: str | None = None, session_factory=None,
           web3=None, log=print) -> dict:
    """Buy everything this invoice needs.

    Returns {receipts, blocked, reason, held, held_reason}. `blocked` is terminal (an
    exact sanctions hit); `held` is an operator decision (fuzzy candidates only).
    """
    receipts = []
    blocked, reason = False, None
    held, held_reason = False, None
    for slug, params in plan_for(invoice):
        session = session_factory(slug) if session_factory else None
        rec = purchase(slug, params, invoice["id"], key=key, session=session,
                       web3=web3, log=log)
        receipts.append(rec)
        if rec.get("ok") and slug.startswith("sanctions"):
            decision, why = screen_verdict(slug, rec.get("result") or {})
            # Name the exact string that was screened: a bare first name reading as a
            # hit is indistinguishable from a real one without it.
            subject = params.get("name") or params.get("address") or ""
            where = f" for {subject!r}" if subject else ""
            if decision == BLOCK:
                blocked, reason = True, f"{why}{where}"
            elif decision == HOLD and not blocked:
                held, held_reason = True, f"{why}{where}"
    spent = sum(float(r.get("price_usd_quoted") or 0) for r in receipts if r.get("ok"))
    return {"receipts": receipts, "blocked": blocked, "reason": reason,
            "held": held, "held_reason": held_reason,
            "spent_usd": round(spent, 4), "at": int(time.time())}



def fx_crosscheck(enrichment: dict, priced_usd_per_unit: str | None) -> dict | None:
    """Compare the paid rate with the rate we priced the invoice with.

    The bought endpoint answers in the direction we asked (USD→BRL gives BRL per
    USD), while we price in USD per unit, so one side has to be inverted. Doing
    that blindly is how a 5.3 vs 0.18 "mismatch" gets reported as a crisis; the
    reciprocal is only taken when the straight comparison is absurd.
    """
    if not priced_usd_per_unit:
        return None
    for rec in enrichment.get("receipts") or []:
        if rec.get("slug") != "fx-rate" or not rec.get("ok"):
            continue
        rate = (rec.get("result") or {}).get("rate")
        bought = rate
        if isinstance(rate, dict):
            bought = rate.get("rate") or rate.get("value") or rate.get("result")
        try:
            bought_f, priced_f = float(bought), float(priced_usd_per_unit)
        except (TypeError, ValueError):
            return {"bought": bought, "priced": priced_usd_per_unit, "comparable": False}
        if not bought_f or not priced_f:
            return {"bought": bought, "priced": priced_usd_per_unit, "comparable": False}
        inverted = False
        if abs(bought_f - priced_f) / priced_f > 1.0:  # off by >100%: unit flip
            bought_f = 1 / bought_f
            inverted = True
        drift = abs(bought_f - priced_f) / priced_f * 100
        return {"bought": round(bought_f, 8), "priced": round(priced_f, 8),
                "bought_raw": bought, "inverted": inverted,
                "drift_pct": round(drift, 3), "comparable": True,
                "agrees": drift < 2.0, "tx": rec.get("tx")}
    return None
