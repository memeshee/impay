"""Telegram bot: sentence -> Jev parse -> confirm -> invoice + trust score.

/start, /new <sentence> (or just send the sentence), /pay INV-0001, /receipt INV-0001
Settlement wiring (x402/Textile/buy) lands in their modules; bot calls them.

Security fixes (audit run-1):
- screen-fail-open-mintable-draft: a screening failure now refuses the draft,
  and only a "settle" verdict reaches the mint button. "review" is a real hold.
- unbound-settlement-proof / settled-receipt-rewrite-on-recheck: /check scans
  from the invoice's creation block, skips witnesses already consumed by another
  invoice, and refuses to touch an already-settled invoice.
- check-unguarded-payment-request / unpriceable-amount-candidates-minted: an
  amount/currency the bot cannot price is refused at draft time, and /check
  wraps pricing the same way /pay does.
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import jev, store

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("invoice-agent")

# Where a payer opens the invoice. The bot hands out a link rather than raw
# address instructions, so a payer who is not in this chat can still pay.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8088").rstrip("/")


def pay_url(inv_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/i/{inv_id}"

# (chat_id, user_id) -> draft awaiting confirm. Keying by chat alone let any
# member of a group act on another member's draft.


def _pct(value) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "n/a"


def fmt_draft(d: dict, s: dict) -> str:
    lines = [
        "Invoice draft",
        f"Amount: {d.get('amount')} {d.get('currency') or ''}",
        f"Recipient: {d.get('recipient') or '—'}",
        f"Trust: {s.get('decision')} (risk {s.get('risk_level')}, "
        f"suspicion {_pct(s.get('suspicious_p'))})",
    ]
    reasons = jev.trust_reasons(s)
    if reasons:
        lines.append("Why: " + "; ".join(reasons))
    return "\n".join(lines)


def must_msg(update: Update) -> Message:
    msg = update.message
    assert msg is not None, "handler needs a message update"
    return msg


def _user_id(user) -> int:
    return int(getattr(user, "id", 0) or 0)


def _creator(msg) -> str:
    """Stable creator handle; `msg.from_user` can be absent on some updates."""
    user = getattr(msg, "from_user", None)
    return str(getattr(user, "username", None) or getattr(user, "id", 0) or 0)


HELP_TEXT = (
    "*impay* — send one sentence, get a payable invoice on Celo.\n"
    "\n"
    "*Try it right now* (nothing to install):\n"
    "`charge Marco 500 BRL for the logo, due Friday`\n"
    "I read it into amount, currency and counterparty, buy the sanctions screens\n"
    "for that invoice over x402, and reply with a link anyone can pay.\n"
    "\n"
    "*Setup* (once):\n"
    "`/wallet 0xYourAddress` — where you get paid. Add an asset for in-kind:\n"
    "`/wallet 0xYourAddress wBRL` (or USAT, wARS, wMXN, wCOP, wPEN, wCLP).\n"
    "\n"
    "*Commands*\n"
    "`/start` this message · `/help` this message\n"
    "`/wallet` show or set the payout address\n"
    "`/new <sentence>` same as just typing the sentence\n"
    "`/pay <id>` pay link + amount for an invoice\n"
    "`/check <id>` re-check the chain now instead of waiting for the watcher\n"
    "`/receipt <id>` what the agent bought for that invoice\n"
    "`/held` invoices waiting on a human · `/release <id>` · `/drop <id>`\n"
    "\n"
    "Settlement is proven from Celo, never from a claim: [/proof]({proof})\n"
    "A sanctions hit blocks the invoice until an operator clears it."
)


async def start(update: Update, _ctx) -> None:
    await must_msg(update).reply_text(
        HELP_TEXT.format(proof=PUBLIC_BASE_URL.rstrip("/") + "/proof"),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, _ctx) -> None:
    await start(update, _ctx)


async def unknown(update: Update, _ctx) -> None:
    """A command we do not have must not answer with silence."""
    cmd = (must_msg(update).text or "").split()[0]
    await must_msg(update).reply_text(
        f"I don't know {cmd}. /help lists everything I can do.")


async def new_invoice(update: Update, ctx) -> None:
    msg = must_msg(update)
    text = " ".join(ctx.args) if ctx.args else (msg.text or "")
    await handle_text(update, text, ctx)


async def handle_text(update: Update, text: str, ctx=None) -> None:
    msg = must_msg(update)
    try:
        draft = jev.parse_invoice(text)
    except Exception as e:  # noqa: BLE001 — surface, don't die in chat
        log.exception("parse failed")
        await msg.reply_text(f"Couldn't parse that ({e}). Try: charge <who> <amount> <ccy>.")
        return
    if not draft.get("is_invoice"):
        await msg.reply_text("That doesn't look like an invoice. Try: charge <who> <amount> <ccy>.")
        return
    if not draft.get("amount"):
        await msg.reply_text(
            "I couldn't find a single amount I can price in that sentence. "
            "Try: charge <who> <amount> <ccy> (e.g. charge Marco 500 BRL)."
        )
        return
    from . import x402
    # What this freelancer wants to be paid in decides the settlement asset, so
    # price the draft with that asset from the start — not after the payer has
    # already been shown a number.
    pref = store.pref_for(_user_id(msg.from_user))
    settle_asset = x402.settle_asset_for(draft.get("currency"), pref.get("asset"))
    priceable, detail = x402.can_price(draft.get("amount"), draft.get("currency"),
                                       settle_asset)
    if not priceable:
        await msg.reply_text(
            f"Can't price that ({detail}). Settleable today: USDT, USDC, USAT, "
            "or BRL/ARS/MXN/COP/USD which settle in USA₮."
        )
        return
    try:
        screen = jev.screen_invoice(draft, store.recent_descriptions())
    except Exception:  # noqa: BLE001 — screening is a gate, not an ornament
        log.exception("screen failed")
        await msg.reply_text(
            "Screening is unavailable right now, so I won't mint this draft. "
            "Try again in a moment."
        )
        return
    decision = screen.get("decision")
    if decision == "block":
        await msg.reply_text(
            f"Blocked: looks risky (suspicion {screen['suspicious_p']:.2f}). "
            "Refine it or talk to a human."
        )
        return
    if decision != "settle":
        # "review" is a hold, not a dead end: park it as a ticket an operator can
        # actually release, instead of telling the user a human will help and
        # giving that human nothing to act on.
        held = store.create(draft, screen,
                            creator=_creator(msg),
                            created_block=None,
                            payout_address=store.wallet_for(_user_id(msg.from_user)),
                            creator_chat_id=msg.chat_id,
                            status="held")
        reasons = jev.trust_reasons(screen)
        await msg.reply_text(
            f"Held for review as {held['id']} (risk {screen.get('risk_level')}).\n"
            f"{('Why: ' + '; '.join(reasons) + chr(10)) if reasons else ''}"
            "An operator can release it with /release " + held["id"] + ", "
            "or drop it with /drop " + held["id"] + "."
        )
        await ping_operator(ctx, held, reasons)
        return
    # The screener already decided; asking for a tap on every clean invoice is
    # friction on the happy path and the thing a first-time user stalls on. Mint
    # immediately, and keep the button for the cases where the verdict is a hold.
    item = {
        "draft": draft,
        "screen": screen,
        "message_id": getattr(msg, "message_id", None),
        "chat_id": msg.chat_id,
        "user_id": _user_id(msg.from_user),
        "settle_asset": settle_asset,
    }
    await mint_and_reply(msg, item, user_id=_user_id(msg.from_user))


def _bare_name_hint(recipient: str | None) -> str:
    """Warn before the money is spent, not after the invoice is blocked.

    The sanctions-name vendor screens the exact string that is sent, so a single
    bare token ("Jack") reads as a match on many lists while "Jack Miller" comes
    back clean. Minting first and explaining after costs the screening fee and
    leaves a dead pay link.
    """
    name = (recipient or "").strip()
    if not name or name.startswith("@") or len(name.split()) > 1:
        return ""
    return ("\n\nNote: the recipient is a bare first name, and the name screen runs on "
            "the exact string — single tokens often read as a match and block the "
            "invoice. If that happens, send it again with the full name.")


async def mint_and_reply(msg, item: dict, user_id: int, creator: str | None = None):
    """Create the invoice and send the pay link. Shared by the chat and the button."""

    from . import x402
    inv = store.create(item["draft"], item["screen"],
                       creator=creator or str(user_id),
                       created_block=x402.head_block(),
                       # The freelancer's own wallet, so a second user can be
                       # paid without an operator editing the env.
                       payout_address=store.wallet_for(user_id),
                       creator_chat_id=item.get("chat_id") or getattr(msg, "chat_id", None) or user_id,
                       settle_asset=item.get("settle_asset"))
    if inv is None:
        return None
    try:
        req = x402.create_payment_request(inv, store.payout_for(inv))
        settle = f"{req['amount_display']} {req['symbol']}"
        if req.get("fx"):
            settle += f" (from {req['invoice_amount_display']})"
    except (ValueError, KeyError) as e:
        settle = f"<unpriceable: {e}>"
    await msg.reply_text(
        f"Minted {inv['id']}\n"
        f"{inv['amount']} {inv['currency']} → {inv['recipient']}\n"
        f"Payer sends: {settle}\n"
        f"Trust: {inv['trust']['decision']} ({inv['trust']['risk_level']})\n\n"
        f"Pay link (send it to your client):\n{pay_url(inv['id'])}\n\n"
        f"The agent now buys its own screening for this invoice\n"
        f"(rate check + sanctions on the wallet and name, ≈$0.009) and watches\n"
        f"Celo to settle it by itself. /receipt {inv['id']}"
        + _bare_name_hint(inv["recipient"])
    )


async def pay(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not ctx.args:
        await msg.reply_text("Usage: /pay INV-0001")
        return
    inv = store.get(ctx.args[0])
    if not inv:
        await msg.reply_text("Unknown invoice.")
        return
    if inv["status"] == "settled":
        await msg.reply_text(f"{inv['id']} already settled: {inv['settlement_tx']}")
        return
    from . import x402
    wallet = store.payout_for(inv)
    if not wallet:
        await msg.reply_text(
            f"{inv['id']} is minted but has no payout address yet.\n"
            "Set yours with /wallet 0x... then ask for /pay again."
        )
        return
    try:
        req = x402.create_payment_request(inv, wallet)
    except (ValueError, KeyError) as e:
        await msg.reply_text(f"Can't price that: {e}")
        return
    detail = f"Pay {req['amount_display']} {req['symbol']} on Celo"
    if req.get("fx"):
        detail += f" (invoice {req['invoice_amount_display']})"
    await msg.reply_text(
        f"{detail}\n\n{pay_url(inv['id'])}\n\n"
        f"Address: `{req['pay_to']}`\n"
        f"Token: `{req['asset_contract']}`\n"
        "The page confirms on its own once the transfer lands; /check forces it."
    )


async def check(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not ctx.args:
        await msg.reply_text("Usage: /check INV-0001")
        return
    inv = store.get(ctx.args[0])
    if not inv:
        await msg.reply_text("Unknown invoice.")
        return
    if inv["status"] != "open":
        # Held, blocked and dropped invoices must not be settleable by hand: the
        # whole point of holding one is that the money must not move.
        await msg.reply_text(
            f"{inv['id']} is '{inv['status']}', so it cannot be settled. "
            "A held invoice needs an operator to /release it first."
        )
        return
    from . import watch as watch_mod
    from . import x402
    # The watcher refuses to settle an unscreened invoice; /check is the same money
    # question, so it must give the same answer.
    if not watch_mod.screening_allows(inv):
        await msg.reply_text(
            f"{inv['id']}: the agent's sanctions screen has not finished "
            f"(status: {inv.get('services_status')}), so it will not settle yet. "
            "The watcher retries the purchase; /check will work once it succeeds."
        )
        return
    wallet = store.payout_for(inv)
    if not wallet:
        await msg.reply_text("No payout address on this invoice yet (/wallet 0x...).")
        return
    try:
        req = x402.create_payment_request(inv, wallet)
    except (ValueError, KeyError) as e:
        await msg.reply_text(f"Can't price that: {e}")
        return
    try:
        hits = x402.watch_transfers(
            wallet, req["symbol"], min_atomic=req["amount_atomic"],
            # Anchor the window at the invoice's creation block; without it a
            # transfer mined before the invoice existed can serve as its proof.
            from_block=inv.get("created_block") or "latest",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("watch failed")
        await msg.reply_text(f"Chain lookup hiccup ({e}). Try /check again.")
        return
    # A witness already credited to another invoice cannot settle this one.
    fresh = [h for h in hits if not store.tx_used(h["tx"])]
    if not fresh:
        await msg.reply_text(
            f"{inv['id']}: no {req['symbol']} received yet. "
            f"Send {req['amount_display']} {req['symbol']} to `{wallet}`, then /check again."
        )
        return
    # /check used to trust the same mechanical filter the watcher had to stop
    # trusting: recipient + token + amount does not prove someone else paid. One
    # definition of "was this paid", or the two paths disagree about the truth.
    from . import verify
    witness = None
    refusal = ""
    for candidate in sorted(fresh, key=lambda h: h.get("block") or 0):
        proof = verify.payment_proof(inv, tx=candidate["tx"])
        if proof["ok"]:
            witness = candidate
            break
        refusal = proof["reason"]
    if witness is None:
        await msg.reply_text(
            f"{inv['id']}: a transfer was found but it does not prove payment — {refusal}. "
            "Nothing was settled."
        )
        return
    inv_id = inv["id"]
    settled = store.mark_settled(inv_id, witness["tx"])
    if settled is None:
        await msg.reply_text(
            f"{inv_id}: that transfer already settled another invoice. "
            "Ask the payer to send a fresh transfer."
        )
        return
    await msg.reply_text(
        f"{inv_id} SETTLED ✅\nTx: `{settled['settlement_tx']}`"
    )


def services_block(inv: dict) -> str:
    """What the agent bought for this invoice, for /receipt."""
    receipts = inv.get("services") or []
    status = inv.get("services_status")
    if not receipts:
        return f"\nServices bought: {status or 'none yet'}"
    lines = [f"\nServices bought (${inv.get('services_spent_usd') or 0}):"]
    for r in receipts:
        if not r.get("ok"):
            lines.append(f"  · {r.get('slug')} — failed ({r.get('error')})")
            continue
        verdict = (r.get("result") or {}).get("verdict") or "ok"
        tx = (r.get("tx") or "")[:12]
        verified = "✓onchain" if (r.get("onchain") or {}).get("verified") else "unverified"
        lines.append(f"  · {r.get('slug')} — {verdict} ({verified}) {tx}…")
    if inv.get("status") == "blocked":
        lines.append(f"BLOCKED: {inv.get('blocked_reason')}")
    return "\n".join(lines)


async def receipt(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not ctx.args:
        await msg.reply_text("Usage: /receipt INV-0001")
        return
    inv = store.get(ctx.args[0])
    if not inv:
        await msg.reply_text("Unknown invoice.")
        return
    await msg.reply_text(
        f"{inv['id']} — {inv['status']}\n"
        f"{inv['amount']} {inv['currency']} → {inv['recipient']}\n"
        f"Trust: {inv['trust']['decision']} ({inv['trust']['risk_level']})\n"
        f"Settlement: {inv['settlement_tx'] or 'pending'}"
        f"{services_block(inv)}\n\n"
        f"{pay_url(inv['id'])}"
    )


async def wallet(update: Update, ctx) -> None:
    """Register where this user gets paid — the multi-tenant unlock.

    Without a per-user payout address every invoice paid the operator's wallet,
    so a second freelancer could not be onboarded at all.
    """
    msg = must_msg(update)
    if not ctx.args:
        current = store.pref_for(_user_id(msg.from_user))
        await msg.reply_text(
            "Usage: /wallet 0xYourCeloAddress [USAT|wBRL|wARS|wMXN|wCOP|wPEN|wCLP]\n"
            f"Current: {current.get('address') or 'not set (falls back to the operator wallet)'}"
            + (f" · {current['asset']}" if current.get("asset") else "")
            + "\nPick wBRL to be paid straight in reais — no dollar in the middle."
        )
        return
    address = ctx.args[0].strip()
    asset = ctx.args[1].strip() if len(ctx.args) > 1 else None
    if asset:
        # Accept "wbrl"/"WBRL" the way a person types it, store canonical casing.
        from . import x402
        asset = next((s for s in x402.ASSETS if s.lower() == asset.lower()), asset)
    try:
        store.set_wallet(_user_id(msg.from_user), address, asset)
    except ValueError as e:
        await msg.reply_text(f"{e}.")
        return
    suffix = f"\nSettlement asset: {asset} (local currency, no FX)" if asset else ""
    await msg.reply_text(f"Payout wallet set to {address}{suffix}\nNew invoices pay you directly on Celo.")


def operator_ids() -> set[str]:
    """Telegram ids allowed to release held invoices. Empty set fails closed."""
    raw = os.environ.get("OPERATOR_TELEGRAM_ID", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _is_operator(update: Update) -> bool:
    """Fail closed: an unset operator list means nobody can release anything.

    Falls back to the message author because some update shapes (and every test
    harness) carry the user on the message rather than on the update.
    """
    ops = operator_ids()
    if not ops:
        return False
    user = getattr(update, "effective_user", None)
    if user is None:
        user = getattr(getattr(update, "message", None), "from_user", None)
    return user is not None and str(getattr(user, "id", "")) in ops


async def ping_operator(ctx, inv: dict, reasons: list[str]) -> None:
    """Tell a human a decision is waiting on them. Best-effort, never fatal."""
    bot = getattr(ctx, "bot", None)
    if bot is None or not operator_ids():
        return
    text = (f"Held for review: {inv['id']}\n"
            f"{inv.get('amount')} {inv.get('currency')} → {inv.get('recipient')}\n"
            + (f"Why: {'; '.join(reasons)}\n" if reasons else "")
            + f"/release {inv['id']}   /drop {inv['id']}")
    for op in operator_ids():
        try:
            await bot.send_message(chat_id=int(op), text=text)
        except Exception:  # noqa: BLE001 — a ping failure must not lose the ticket
            log.exception("operator ping failed")


async def release(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not _is_operator(update):
        await msg.reply_text("Only an operator can release a held invoice.")
        return
    if not ctx.args:
        await msg.reply_text("Usage: /release INV-0007-ABCDEF")
        return
    from . import x402
    inv = store.release(ctx.args[0], payout_address=None, created_block=x402.head_block())
    if not inv:
        await msg.reply_text(f"No held invoice {ctx.args[0]}.")
        return
    await msg.reply_text(f"Released {inv['id']}\n{pay_url(inv['id'])}")
    if inv.get("creator_chat_id") and getattr(ctx, "bot", None) is not None:
        try:
            await ctx.bot.send_message(
                chat_id=inv["creator_chat_id"],
                text=f"{inv['id']} was cleared by an operator and can be paid now:\n"
                     f"{pay_url(inv['id'])}")
        except Exception:  # noqa: BLE001
            log.exception("could not notify the invoice creator of the release")


async def drop(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not _is_operator(update):
        await msg.reply_text("Only an operator can drop a held invoice.")
        return
    if not ctx.args:
        await msg.reply_text("Usage: /drop INV-0007-ABCDEF")
        return
    inv = store.drop(ctx.args[0])
    await msg.reply_text(f"Dropped {inv['id']}." if inv else f"No held invoice {ctx.args[0]}.")


async def held(update: Update, ctx) -> None:
    msg = must_msg(update)
    if not _is_operator(update):
        await msg.reply_text("Only an operator can list held invoices.")
        return
    tickets = store.held_invoices()
    if not tickets:
        await msg.reply_text("Nothing held.")
        return
    lines = ["Held:"]
    for inv in tickets:
        reasons = jev.trust_reasons(inv.get("trust") or {})
        lines.append(f"  {inv['id']} — {inv['amount']} {inv['currency']} → {inv.get('recipient')}"
                     + (f" ({'; '.join(reasons)})" if reasons else ""))
    await msg.reply_text("\n".join(lines))


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("new", new_invoice))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("receipt", receipt))
    app.add_handler(CommandHandler("release", release))
    app.add_handler(CommandHandler("drop", drop))
    app.add_handler(CommandHandler("held", held))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, new_invoice))
    # Last in the chain: anything that is still a command is one we do not implement.
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.run_polling()


if __name__ == "__main__":
    main()
