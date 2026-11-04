"""Autonomous settlement watcher — the part that makes this an agent.

Before this, settlement only happened when a human typed `/check`. That makes
the bot a parser with a cron habit, not an agent: nothing closed the loop on its
own, and a payer who simply sent the money left the invoice dangling.

This loop polls Celo for every open invoice, settles the ones that were paid,
and pushes the receipt to the freelancer.

Why Blockscout and not eth_getLogs: free Celo RPCs cannot serve day-scale log
history (forno rejects topic filters outright, publicnode caps a request at
~3000 blocks, ankr refuses large ranges). A head-relative log window silently
stops finding payments as an invoice ages — the exact failure that makes a
settled invoice read as unpaid.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from . import services, store, verify, x402

load_dotenv()

BLOCKSCOUT_V1 = "https://celo.blockscout.com/api"
BLOCKSCOUT_V2 = "https://celo.blockscout.com/api/v2"
POLL_INTERVAL_S = int(os.environ.get("WATCH_INTERVAL_S", "45"))
UA = "celo-invoice-agent/1.0"


def _open(url: str, timeout: int = 25):
    """HTTP GET seam. Exists so the retry path is testable without the network."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def _is_retryable(exc: Exception) -> bool:
    """429/5xx and transport errors are worth another go. A 404 is not.

    HTTPError subclasses URLError, so testing URLError first would retry every
    HTTP status — including the 404 that means "this endpoint is gone, try the
    other one now".
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


def _get_json(url: str, *, attempts: int = 3, backoff_s: float = 2.0, log=print):
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with _open(url) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_retryable(e) or attempt == attempts:
                raise
            log(f"[watch] fetch {attempt}/{attempts} failed ({type(e).__name__}); "
                f"retrying in {backoff_s * attempt:.0f}s")
            time.sleep(backoff_s * attempt)
    raise last or RuntimeError("unreachable")


def _parse_v2(body: dict) -> list[dict]:
    """Blockscout v2 token-transfers shape."""
    if "items" not in body:
        raise ValueError("not a v2 token-transfers payload")
    out = []
    for it in body.get("items") or []:
        total = it.get("total") or {}
        value = total.get("value") if isinstance(total, dict) else None
        if value is None:
            continue
        out.append({
            "tx": str(it.get("transaction_hash") or "").lower(),
            "from": str((it.get("from") or {}).get("hash") or "").lower(),
            "to": str((it.get("to") or {}).get("hash") or "").lower(),
            "contract": str((it.get("token") or {}).get("address_hash") or "").lower(),
            "value": int(value),
            "block": int(it.get("block_number") or 0),
        })
    return [t for t in out if t["tx"] and t["to"] and t["contract"]]


def _parse_v1(body: dict) -> list[dict]:
    """Blockscout v1 (etherscan-compatible) tokentx shape."""
    if "result" not in body and str(body.get("status")) != "1":
        raise ValueError("not a v1 tokentx payload")
    if str(body.get("status")) != "1":
        return []
    out = []
    for t in body.get("result") or []:
        try:
            out.append({
                "tx": str(t["hash"]).lower(),
                "from": str(t.get("from") or "").lower(),
                "to": str(t["to"]).lower(),
                "contract": str(t["contractAddress"]).lower(),
                "value": int(t["value"]),
                "block": int(t["blockNumber"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _transfers_v2(address: str, **kw) -> list[dict]:
    return _parse_v2(_get_json(
        f"{BLOCKSCOUT_V2}/addresses/{address}/token-transfers?type=ERC-20", **kw))


def _transfers_v1(address: str, **kw) -> list[dict]:
    query = urllib.parse.urlencode({
        "module": "account", "action": "tokentx", "address": address,
        "offset": "100", "sort": "desc",
    })
    return _parse_v1(_get_json(f"{BLOCKSCOUT_V1}?{query}", **kw))


def transfers_to(address: str, *, fetcher=None, attempts: int = 3,
                 backoff_s: float = 2.0, log=print) -> list[dict]:
    """Every ERC-20 transfer into `address`, newest first, normalized.

    Two sources on purpose. Blockscout's v1 endpoint rate-limits this machine
    into a hard 429 while v2 keeps answering (they are metered separately), and a
    failed fetch must never be read as "nobody paid" — that is exactly how a
    settled invoice stays open forever. So: retry with backoff, then fail over,
    then raise so the caller can say "could not ask" instead of "nothing there".
    """
    if fetcher is not None:
        return fetcher(address)
    errors = []
    for name, fetch in (("v2", _transfers_v2), ("v1", _transfers_v1)):
        try:
            return fetch(address, attempts=attempts, backoff_s=backoff_s, log=log)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
            log(f"[watch] {name} source failed for {address[:10]}… "
                f"({type(e).__name__}: {e})")
    raise RuntimeError("all transfer sources failed — " + ", ".join(errors))


def _enrich_enabled() -> bool:
    """Is the paid screening actually switched on for this deployment?"""
    return str(os.environ.get("SERVICES_ENRICH", "1")).strip().lower() not in ("0", "false", "no")


def screening_allows(invoice: dict) -> bool:
    """Has the paid screening actually FINISHED, or is it deliberately off?

    Only "done" clears an invoice. Treating a failed screen ("error") as clearance
    meant a purchase that crashed blocked nothing at all — the card on chain says a
    sanctions hit stops settlement, so an unscreened invoice must not settle.

    Invoices are created with services_status="pending" and stay unsettleable until
    the agent has bought its screens. With enrichment disabled there is no screen to
    wait for, and "pending" is just the creation default.
    """
    if not _enrich_enabled():
        return True
    return (invoice.get("services_status") or "") == "done"


def find_settlement(invoice: dict, transfers: list[dict], *, confirm=None) -> dict | None:
    """The transfer that settles this invoice, or None.

    Oldest qualifying transfer wins, so a payer who sent twice does not have the
    later one bound to an invoice the first one already paid.

    `confirm` is the real gate: mechanical matching (recipient, asset, amount,
    block) is necessary but not sufficient — the candidate must also survive
    bot.verify.payment_proof, which checks the transaction initiator. Without it a
    swap the payee made itself settles its own invoice.
    """
    pay_to = store.payout_for(invoice)
    if not pay_to:
        return None
    # The agent buys its screening before it is allowed to decide anything.
    # Settling while that screen is still running would let a sanctions hit arrive
    # after the money did, which is the one order that cannot be undone.
    if invoice.get("status") != "open":
        return None
    if not screening_allows(invoice):
        return None
    try:
        req = x402.create_payment_request(invoice, pay_to)
    except (ValueError, KeyError, RuntimeError):
        return None  # unpriceable now (e.g. FX outage): try again next sweep
    asset = req["asset_contract"].lower()
    needed = int(req["amount_atomic"])
    floor = int(invoice.get("created_block") or 0)
    hits = [
        t for t in transfers
        if t["to"] == pay_to.lower()
        and t["contract"] == asset
        and t["value"] >= needed
        and t["block"] >= floor
        and store.tx_used(t["tx"]) is None
        # Funds arriving from the payee's own wallet are not a payment: our own
        # swaps and top-ups land here too.
        and (t.get("from") or "").lower() != pay_to.lower()
    ]
    for cand in sorted(hits, key=lambda t: t["block"]):
        if confirm is None or confirm(cand):
            return cand
    return None


def notify(chat_id: int | None, text: str, *, sender=None) -> bool:
    """Push to Telegram. Never raises: a failed ping must not unpick a settlement."""
    if not chat_id:
        return False
    if sender is not None:
        return sender(chat_id, text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                      "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=payload, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001 — logging is the point, not the raise
        print(f"[watch] notify failed: {type(e).__name__}: {e}", flush=True)
        return False


def enrich_pending(*, log=print, enricher=None, notifier=None,
                   max_per_loop: int | None = None) -> list[dict]:
    """Buy the services each pending invoice needs, then record what was bought.

    Runs in the same daemon as settlement so there is one moving part, and
    capped per loop so a burst of mints cannot drain the wallet in one pass.

    `notifier` is injectable for the same reason `enricher` is: the offline test
    suite must not reach api.telegram.org to prove that a notification happens.
    """
    if os.environ.get("SERVICES_ENRICH", "1") != "1":
        return []
    cap = max_per_loop if max_per_loop is not None else int(os.environ.get("ENRICH_MAX_PER_LOOP", "3"))
    done = []
    for inv in store.invoices_awaiting_services()[:cap]:
        try:
            enrichment = (enricher or services.enrich)(inv)
        except Exception as e:  # noqa: BLE001
            log(f"[services] enrichment crashed for {inv['id']}: {type(e).__name__}: {e}")
            # "retry", not "error": a crashed screen must be retried, and until it
            # succeeds the invoice is not settleable.
            store.set_services(inv["id"], "retry")
            continue
        # A failure to buy does not block settlement forever — it blocks it until
        # a later loop succeeds, which is what "retry" means to the sweeper.
        status = "done" if any(r.get("ok") for r in enrichment["receipts"]) else "retry"
        updated = store.set_services(inv["id"], status, enrichment)
        done.append(updated or {})
        spent = enrichment.get("spent_usd")
        bought = [r["slug"] for r in enrichment["receipts"] if r.get("ok")]
        log(f"[services] {inv['id']} bought {bought} for ${spent}"
            + (" — BLOCKED: " + str(enrichment.get('reason')) if enrichment["blocked"] else ""))
        if updated:
            if enrichment["blocked"]:
                # store.release() only accepts `held`, so promising a human here
                # would be a refusal that points at a path which does not exist.
                msg = (f"{inv['id']} blocked ❌\n"
                       f"{enrichment.get('reason')}\n"
                       "A sanctions match is terminal for this invoice — an operator "
                       "cannot clear it. Send a new one; if the recipient was a bare "
                       "first name, use the full name, because the screen runs on the "
                       "exact string you typed.")
            else:
                msg = (f"{inv['id']} screened ✅\n"
                       f"Bought {len(bought)} services for ${spent}: {', '.join(bought)}\n"
                       f"Payout wallet and counterparty came back clean.")
            (notifier or notify)(updated.get("creator_chat_id"), msg)
    return done


def _confirm(invoice: dict, candidate: dict, *, log=print, web3=None) -> bool:
    """Mechanical match is not proof — ask bot.verify about the initiator."""
    proof = verify.payment_proof(invoice, tx=candidate["tx"], web3=web3)
    if not proof["ok"]:
        log(f"[watch] rejected witness {candidate['tx'][:18]}… for {invoice['id']}: "
            f"{proof['reason']}")
    return bool(proof["ok"])


def sweep(*, fetcher=None, notifier=None, confirm=None, log=print) -> list[dict]:
    """One pass over every open invoice. Returns settlements made."""
    settled = []
    open_invs = store.open_invoices()
    if not open_invs:
        return settled
    cache: dict[str, list[dict]] = {}
    for inv in open_invs:
        pay_to = store.payout_for(inv)
        if not pay_to:
            continue
        if pay_to.lower() not in cache:
            try:
                cache[pay_to.lower()] = transfers_to(pay_to, fetcher=fetcher)
            except Exception as e:  # noqa: BLE001
                log(f"[watch] fetch failed for {pay_to}: {type(e).__name__}: {e}")
                continue
        hit = find_settlement(inv, cache[pay_to.lower()],
                              confirm=confirm or (lambda c, _inv=inv: _confirm(_inv, c, log=log)))
        if not hit:
            continue
        done = store.mark_settled(inv["id"], hit["tx"])
        if done is None:
            continue
        settled.append(done)
        log(f"[watch] {inv['id']} settled by {hit['tx']}")
        notify(done.get("creator_chat_id"),
               f"{done['id']} settled ✅\n"
               f"{done['amount']} {done['currency']} → {done['recipient']}\n"
               f"tx: {hit['tx']}\n"
               f"https://celo.blockscout.com/tx/{hit['tx']}", sender=notifier)
    return settled


def main() -> None:
    print(f"[watch] polling every {POLL_INTERVAL_S}s", flush=True)
    while True:
        try:
            enrich_pending()
        except Exception as e:  # noqa: BLE001
            print(f"[watch] enrichment pass crashed: {type(e).__name__}: {e}", flush=True)
        try:
            sweep()
        except Exception as e:  # noqa: BLE001 — the loop must outlive any single pass
            print(f"[watch] sweep crashed: {type(e).__name__}: {e}", flush=True)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
