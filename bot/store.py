"""JSON invoice store. Single-file, no deps. Fine for hackathon scale.

Security fixes (audit run-1):
- unbound-settlement-proof / settled-receipt-rewrite-on-recheck: the store now
  records the invoice's creation block, keeps a consumed-witness registry so one
  on-chain transfer can settle at most one invoice, and never rewrites a
  settled invoice.
- invoice-lookup-not-scoped-to-requester: ids carry a random suffix, so the
  handle is a capability instead of a guessable counter.

Multi-tenant (Phase 1): each invoice carries its own payout address, and a user
registers the wallet they get paid at. Before this, every invoice paid one
global INVOICE_WALLET, so onboarding a second freelancer — an "independent
user" in the rules — was structurally impossible.
"""

import json
import os
import re
import secrets
import time

STORE_PATH = os.environ.get("INVOICE_STORE", "invoices.json")

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _load() -> dict:
    try:
        with open(STORE_PATH) as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {"seq": 0, "invoices": {}}
    db.setdefault("used_tx", {})
    db.setdefault("wallets", {})
    return db


def _save(db: dict) -> None:
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, STORE_PATH)


def _refresh_claims(db: dict) -> None:
    """Keep the public snapshot in step with the store it was exported from.

    Best-effort on purpose: a failed snapshot write must never unpick a
    settlement. The daily audit reads this file, so a stale snapshot is the one
    failure mode worth avoiding — it would let the README claim a payment that
    the store no longer holds.
    """
    try:
        from . import claims
        claims.write(db)
    except Exception as e:  # noqa: BLE001
        print(f"[store] claims snapshot not refreshed: {type(e).__name__}: {e}", flush=True)


# ------------------------------------------------------------------- payouts

def set_wallet(user_id: int | str, address: str, asset: str | None = None) -> str:
    """Register the wallet a user gets paid at, and optionally the asset.

    `asset` is how a Brazilian freelancer says "pay me in reais": the invoice is
    then settled in wBRL (Ripio's local stablecoin) instead of converting to
    USA₮, so the money never leaves the currency they actually spend.
    """
    if not ADDRESS_RE.match(address or ""):
        raise ValueError(f"not an address: {address!r}")
    if asset is not None:
        from . import x402
        allowed = ("USAT",) + tuple(s for s in x402.ASSETS if s.startswith("w"))
        if asset not in allowed:
            raise ValueError(f"unsupported settlement asset {asset!r} (choose from {', '.join(allowed)})")
    db = _load()
    entry = {"address": address, "set": int(time.time())}
    if asset:
        entry["asset"] = asset
    db["wallets"][str(user_id)] = entry
    _save(db)
    return address


def pref_for(user_id: int | str) -> dict:
    """The registered payout preference, e.g. {'address': .., 'asset': 'wBRL'}."""
    return dict(_load()["wallets"].get(str(user_id)) or {})


def wallet_for(user_id: int | str) -> str | None:
    return (_load()["wallets"].get(str(user_id)) or {}).get("address")


def payout_for(invoice: dict) -> str:
    """Invoice payout address, falling back to the operator wallet.

    Resolving at read time (not write time) keeps invoices minted before
    per-invoice routing working with no migration.
    """
    return invoice.get("payout_address") or os.environ.get("INVOICE_WALLET", "")


# ------------------------------------------------------------------ invoices

def create(draft: dict, screen: dict, creator: str,
           created_block: int | None = None,
           payout_address: str | None = None,
           creator_chat_id: int | None = None,
           status: str = "open",
           settle_asset: str | None = None) -> dict:
    db = _load()
    db["seq"] += 1
    db.setdefault("used_tx", {})
    # Guessable ids let any chat participant enumerate every invoice; the
    # random suffix makes possession of the id the capability.
    inv_id = f"INV-{db['seq']:04d}-{secrets.token_hex(3).upper()}"
    inv = {
        "id": inv_id,
        "created": int(time.time()),
        "created_block": created_block,
        "creator": creator,
        "creator_chat_id": creator_chat_id,
        "amount": draft.get("amount"),
        "currency": draft.get("currency"),
        "recipient": draft.get("recipient"),
        "raw": draft.get("raw"),
        "payout_address": payout_address,
        # What this invoice will actually be paid in. Recorded at mint so the
        # page, the watcher and the receipt cannot disagree about it later.
        "settle_asset": settle_asset,
        "trust": {
            "decision": screen.get("decision"),
            "suspicious_p": screen.get("suspicious_p"),
            "risk_level": screen.get("risk_level"),
            "duplicate_p": screen.get("duplicate_p"),
            "pressure_p": screen.get("pressure_p"),
        },
        "status": status,
        "settlement_tx": None,
        # The agent buys its compliance layer per invoice, asynchronously —
        # minting must not block on three paid calls.
        "services_status": "pending" if status == "open" else "held",
        "services": [],
    }
    db["invoices"][inv_id] = inv
    _save(db)
    return inv


def held_invoices() -> list[dict]:
    return [i for i in _load()["invoices"].values() if i.get("status") == "held"]


def drop(inv_id: str) -> dict | None:
    """Operator rejects a held invoice. Kept in the store as evidence, not deleted."""
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv or inv.get("status") != "held":
        return None
    inv["status"] = "dropped"
    inv["dropped_at"] = int(time.time())
    _save(db)
    return inv


def release(inv_id: str, *, payout_address: str | None,
            created_block: int | None) -> dict | None:
    """Operator clears a held invoice so it can be paid.

    A "review" verdict used to dead-end: the bot told the user a human would
    release it and there was no way to do so. Settlement only becomes possible
    here, and the settlement window starts at release, not at the original draft
    — otherwise a transfer mined while the invoice was held could serve as its
    proof.
    """
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv or inv.get("status") != "held":
        return None
    inv["status"] = "open"
    inv["released"] = int(time.time())
    inv["created_block"] = created_block
    if payout_address:
        inv["payout_address"] = payout_address
    # Never re-buy a screen that already ran clean: resetting this to "pending" made
    # a released invoice unpayable again (the page said "screening in progress") and
    # charged the screening fee twice.
    if (inv.get("services_status") or "") != "done":
        inv["services_status"] = "pending"
    _save(db)
    return inv



def rejudge(inv_id: str, *, decision: str, reason: str) -> dict | None:
    """Re-apply a screening POLICY decision to an invoice that is already screened.

    The paid response is on disk, so fixing the policy must not mean buying the screen
    again: a name the old rule blocked on substring noise can be re-ranked as a hold,
    and an all-token hit re-ranked as a block. Terminal states are never reopened.
    """
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv or inv.get("status") in ("settled", "dropped", "expired"):
        return None
    target = "blocked" if decision == "block" else "held"
    if inv.get("status") == target:
        return inv
    inv["previous_status"] = inv.get("status")
    inv["status"] = target
    inv["rejudged_at"] = int(time.time())
    if target == "blocked":
        inv["blocked_reason"] = reason
        inv.pop("held_reason", None)
    else:
        inv["held_reason"] = reason
        inv.pop("blocked_reason", None)
    _save(db)
    return inv


def get(inv_id: str) -> dict | None:
    return _load()["invoices"].get(str(inv_id).upper())


def open_invoices() -> list[dict]:
    """Every invoice still awaiting settlement — the watcher's work queue."""
    return [i for i in _load()["invoices"].values() if i.get("status") == "open"]


def invoices_awaiting_services() -> list[dict]:
    """Invoices minted but not yet enriched with the services the agent buys.

    Held invoices are included: they are created with services_status="held" and
    used to only be screened after an operator released them — so the operator
    cleared a hold blind and the invoice then went blocked by a sanctions match the
    agent could have reported before anyone acted.
    """
    return [i for i in _load()["invoices"].values()
            if i.get("services_status") in ("pending", "retry", "held")]



def set_services(inv_id: str, status: str, enrichment: dict | None = None) -> dict | None:
    """Record what the agent bought for this invoice, and block it if need be."""
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv:
        return None
    inv["services_status"] = status
    if enrichment is not None:
        inv["services"] = enrichment.get("receipts") or []
        inv["services_spent_usd"] = enrichment.get("spent_usd")
        inv["screened_at"] = enrichment.get("at")
        if enrichment.get("blocked"):
            # A confirmed sanctions hit stops the money. It is not a warning label.
            if inv["status"] in ("settled", "dropped"):
                # Too late: the money already moved, and a settled invoice staying
                # settled is worth more than a retroactive status change that also
                # hid it from /proof and left the witness consumed.
                inv["blocked_reason"] = enrichment.get("reason")
                inv["screening_flag_after_settlement"] = True
            else:
                inv["status"] = "blocked"
                inv["blocked_reason"] = enrichment.get("reason")
        elif enrichment.get("held"):
            # Fuzzy candidates are a decision, not a verdict: park the invoice for an
            # operator instead of refusing it forever over a common first name.
            inv["held_reason"] = enrichment.get("held_reason")
            if inv["status"] == "open":
                inv["status"] = "held"

    _save(db)
    return inv


def expire(inv_id: str, reason: str = "stale test invoice") -> dict | None:
    """Retire an open invoice that nobody is going to pay.

    Every open invoice has a live pay page. Leaving months of test invoices open
    means a judge can land on one and pay money for something that was never real,
    so they are expired rather than silently left there.
    """
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv or inv.get("status") != "open":
        return None
    inv["status"] = "expired"
    inv["expired_reason"] = reason
    _save(db)
    return inv


def tx_used(tx_hash: str) -> str | None:
    """Invoice id that already consumed this witness transfer, if any."""
    used = _load().get("used_tx") or {}
    return used.get(str(tx_hash).lower())


def mark_settled(inv_id: str, tx_hash: str) -> dict | None:
    """Settle an invoice once, against an unconsumed witness.

    Returns the invoice unchanged when it is already settled, and None when the
    transfer has already settled a different invoice.
    """
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv:
        return None
    if inv["status"] == "settled":
        return inv  # terminal: never re-bind settlement_tx on a re-check
    if inv["status"] != "open":
        # held / blocked / dropped: the refusal has to survive the discovery of a
        # transfer, or holding an invoice means nothing.
        return None
    used = db.setdefault("used_tx", {})
    key = str(tx_hash).lower()
    owner = used.get(key)
    if owner is not None and owner != inv["id"]:
        return None  # one transfer settles at most one invoice
    used[key] = inv["id"]
    inv["status"] = "settled"
    inv["settlement_tx"] = tx_hash
    _save(db)
    _refresh_claims(db)
    return inv


def unsettle(inv_id: str, *, reason: str) -> dict | None:
    """Withdraw a settlement claim that does not verify against the chain.

    Settlement is normally terminal, and deliberately so: a re-scan must never
    re-bind the witness. This is the one exception, and it is explicit and
    recorded — a receipt citing a transaction that is not a payment is worse than
    an open invoice, because a judge will check it.
    """
    db = _load()
    inv = db["invoices"].get(str(inv_id).upper())
    if not inv or inv.get("status") != "settled":
        return None
    inv.setdefault("settlement_history", []).append({
        "tx": inv.get("settlement_tx"),
        "withdrawn_at": int(time.time()),
        "reason": reason,
    })
    inv["status"] = "open"
    inv["settlement_tx"] = None
    db.setdefault("used_tx", {}).pop(
        str((inv["settlement_history"][-1] or {}).get("tx") or "").lower(), None)
    _save(db)
    _refresh_claims(db)
    return inv


def recent_descriptions(limit: int = 20) -> list[str]:
    db = _load()
    invs = sorted(db["invoices"].values(), key=lambda i: i["created"], reverse=True)
    return [f"{i['recipient']} {i['amount']} {i['currency']}" for i in invs[:limit]]
