"""Regression tests for the security-audit fixes. Offline: no network, no keys.

Run:  python3 -m pytest tests/test_regressions.py -q
Every test here corresponds to a finding in
~/security-audit-skill/celo-invoice-agent/run-1/findings.json and fails on the
pre-fix code.
"""

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------- import stubs
# bot.main imports python-telegram-bot at module scope; these tests drive the
# handler coroutines directly with stand-in update objects.
def _install_stubs() -> None:
    if "telegram" not in sys.modules:
        tg = types.ModuleType("telegram")
        tg.InlineKeyboardButton = lambda *a, **k: None
        tg.InlineKeyboardMarkup = lambda *a, **k: None
        tg.Message = object
        tg.Update = object
        tg_ext = types.ModuleType("telegram.ext")
        tg_ext.Application = object
        tg_ext.CallbackQueryHandler = object
        tg_ext.CommandHandler = object
        tg_ext.MessageHandler = object
        tg_ext.filters = SimpleNamespace(TEXT=None, COMMAND=None)
        tg.ext = tg_ext
        sys.modules["telegram"] = tg
        sys.modules["telegram.ext"] = tg_ext
    if "dotenv" not in sys.modules:
        dt = types.ModuleType("dotenv")
        dt.load_dotenv = lambda *a, **k: None
        sys.modules["dotenv"] = dt


_install_stubs()

from bot import buy, jev, main, store, x402  # noqa: E402


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _no_screening(monkeypatch):
    """Screening has its own tests; these cover matching and settlement paths."""
    monkeypatch.setenv("SERVICES_ENRICH", "0")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", str(tmp_path / "invoices.json"))
    monkeypatch.setattr(buy, "SPEND_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("INVOICE_WALLET", "0x86E95581E41946ED84956433a8a9c836bCbA636c")
    yield


class Hex:
    """Stands in for web3 HexBytes: .hex() with no 0x prefix."""

    def __init__(self, value: str):
        self._value = value.lower().replace("0x", "")

    def hex(self) -> str:
        return self._value


class FakeEth:
    def __init__(self, logs, head=20000):
        self._logs = list(logs)
        self.head = head
        self.queries = []

    @property
    def block_number(self):
        return self.head

    def get_logs(self, flt):
        self.queries.append(flt)
        return [lg for lg in self._logs
                if flt["fromBlock"] <= lg["blockNumber"] <= flt["toBlock"]]


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


# The canonical keccak of Transfer(address,address,uint256). Pinned as a
# literal on purpose: a test that reads the constant from the module under test
# cannot detect a wrong constant.
TOPIC_CANONICAL = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def transfer_log(to_addr, value, block=15000, topic=TOPIC_CANONICAL,
                 tx="aa" * 32):
    padded_to = "0x" + "0" * 24 + to_addr.lower().replace("0x", "")
    return {"topics": [Hex(topic), Hex("0" * 64), Hex(padded_to)],
            "data": hex(value), "blockNumber": block, "transactionHash": Hex(tx)}


class FlakyEth(FakeEth):
    """First get_logs call answers empty, as a public RPC node sometimes does."""

    def __init__(self, logs, head=20000):
        super().__init__(logs, head=head)
        self.calls = 0

    def get_logs(self, flt):
        self.calls += 1
        if self.calls == 1:
            self.queries.append(flt)
            return []
        return super().get_logs(flt)


def install_chain(monkeypatch, logs, head=20000) -> FakeEth:
    eth = FakeEth(logs, head=head)
    monkeypatch.setattr(x402, "_w3", lambda: FakeW3(eth))
    return eth


class FakeMsg:
    def __init__(self, chat_id=1, user_id=7, text="", message_id=100):
        self.chat_id = chat_id
        self.from_user = SimpleNamespace(id=user_id, username=f"user{user_id}",
                                         full_name=f"user{user_id}")
        self.message_id = message_id
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self

    @property
    def sent(self):
        return "\n".join(self.replies)


def msg_update(text: str, chat_id=1, user_id=7, message_id=100):
    return SimpleNamespace(message=FakeMsg(chat_id, user_id, text, message_id))


def cmd(args, chat_id=1, user_id=7, message_id=100):
    """(update, ctx) pair for the /pay, /check and /receipt handlers."""
    msg = FakeMsg(chat_id, user_id, message_id=message_id)
    return SimpleNamespace(message=msg), SimpleNamespace(args=args)


def run(coro):
    return asyncio.run(coro)


DRAFT = {"is_invoice": True, "amount": "100", "currency": "USDT",
         "recipient": "Marco", "raw": "charge Marco 100 USDT"}


def mint(draft=None, screen=None, creator="user7", created_block=None):
    screen = screen or {"decision": "settle", "risk_level": "clean",
                        "suspicious_p": 0.0}
    return store.create(draft or dict(DRAFT), screen, creator,
                        created_block=created_block)


# --------------------------------------------- 1. malformed Transfer topic
def test_every_accepted_transfer_topic_is_a_real_32_byte_topic():
    assert len(TOPIC_CANONICAL) == 66
    for symbol, topics in x402.TRANSFER_TOPICS.items():
        for topic in topics:
            assert len(topic) == 66, f"{symbol}: {topic} is {len(topic) - 2} bytes"
            assert topic.lower() == TOPIC_CANONICAL


def test_non_usat_settlement_is_detectable(monkeypatch):
    """Pre-fix, the USDT entry could never match a genuine log topic."""
    wallet = "0x86E95581E41946ED84956433a8a9c836bCbA636c"
    install_chain(monkeypatch, [transfer_log(wallet, 100_000_000)])
    hits = x402.watch_transfers(wallet, "USDT", min_atomic=100_000_000,
                                from_block=14000, to_block=16000)
    assert hits and hits[0]["value"] == 100_000_000


def test_threshold_must_be_positive(monkeypatch):
    install_chain(monkeypatch, [])
    with pytest.raises(ValueError):
        x402.watch_transfers("0x86E95581E41946ED84956433a8a9c836bCbA636c",
                             "USAT", min_atomic=0)


def test_empty_window_is_retried_once(monkeypatch):
    """A public RPC node answered an immutable window with [] in practice."""
    wallet = "0x86E95581E41946ED84956433a8a9c836bCbA636c"
    eth = FlakyEth([transfer_log(wallet, 100_000_000)])
    monkeypatch.setattr(x402, "_w3", lambda: FakeW3(eth))
    hits = x402.watch_transfers(wallet, "USAT", min_atomic=100_000_000,
                                from_block=14000, to_block=16000)
    assert eth.calls == 2, "an empty window must be queried twice"
    assert len(hits) == 1 and hits[0]["block"] == 15000


# ------------------------------------------- 2. amount tokenizer + atomic()
@pytest.mark.parametrize("text,expected", [
    ("charge Marco 1.500 BRL for the logo", ["1500"]),
    ("charge Ana 500,00 BRL", ["500.00"]),
    ("pagar 1.500,50 wBRL", ["1500.50"]),
    ("invoice 1,500.50 USD", ["1500.50"]),
    ("total 42", ["42"]),
])
def test_amount_candidates_are_canonical(text, expected):
    assert jev.amount_candidates(text) == expected


def test_grouped_thousands_does_not_shrink_the_invoice():
    """Pre-fix: '1.500' offered the model ['1.50','0'] and priced 1000x low."""
    text = "charge Marco 1.500 BRL for the logo"
    assert "1.50" not in jev.amount_candidates(text)
    assert jev.normalize_amount_token("1.500") == "1500"
    def provider():
        return {"rates": {"BRL": 5.0}, "as_of": "t", "fetched": 0, "source": "t"}
    req = x402.create_payment_request(
        {"id": "INV-1", "amount": "1500", "currency": "BRL"}, "0xabc",
        fx_provider=provider)
    # 1500 BRL at 5.00 BRL/USD is 300 USA₮ — 1000x the value the old tokenizer's
    # ['1.50','0'] candidates would have priced.
    assert req["amount_atomic"] == 300 * 10**6
    assert req["invoice_amount_display"] == "1500 BRL"


@pytest.mark.parametrize("bad", ["-5", "+5", "5_0", "", "?", "1e3", "abc",
                                 "1,5,5", "-0.5"])
def test_atomic_rejects_non_canonical_amounts(bad):
    with pytest.raises(ValueError):
        x402.atomic(bad, "USAT")


def test_canonical_amount_accepts_comma_decimal():
    assert x402.canonical_amount("500,00") == "500.00"
    assert x402.atomic("500,00", "USAT") == 500_000_000


# ----------------------------------- 3. currency pricing for the w* corridor
@pytest.mark.parametrize("currency,expected", [
    ("wBRL", "wBRL"), ("WBRL", "wBRL"), ("wbrl", "wBRL"),
    ("USDT", "USDT"), ("usdc", "USDC"), ("USAT", "USAT"),
    ("BRL", "USAT"), ("ARS", "USAT"), ("USD", "USAT"),
])
def test_currency_resolution_is_case_insensitive(currency, expected):
    assert x402.resolve_symbol(currency) == expected


def test_unknown_currency_fails_closed():
    with pytest.raises(ValueError):
        x402.resolve_symbol("PEN")
    assert x402.can_price("100", "PEN")[0] is False
    assert x402.can_price("100", "wBRL")[0] is True


def test_unpriceable_draft_never_reaches_the_mint_button(monkeypatch):
    monkeypatch.setattr(jev, "parse_invoice", lambda text: {
        "is_invoice": True, "amount": "100", "currency": "PEN",
        "recipient": "Marco", "raw": text})
    update = msg_update("charge Marco 100 PEN")
    run(main.handle_text(update, "charge Marco 100 PEN"))
    assert store.open_invoices() == [], 'nothing may be minted here'
    assert "Can't price that" in update.message.sent


# ------------------------------------------------- 4. screening fails closed
def test_screening_failure_refuses_the_draft(monkeypatch):
    monkeypatch.setattr(jev, "parse_invoice", lambda text: dict(DRAFT, raw=text))
    monkeypatch.setattr(jev, "screen_invoice",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("typesafe down")))
    update = msg_update("charge Marco 100 USDT")
    run(main.handle_text(update, "charge Marco 100 USDT"))
    assert store.open_invoices() == [], 'nothing may be minted here'
    assert "won't mint" in update.message.sent


def test_review_verdict_is_a_hold_not_a_mint(monkeypatch):
    monkeypatch.setattr(jev, "parse_invoice", lambda text: dict(DRAFT, raw=text))
    monkeypatch.setattr(jev, "screen_invoice", lambda *a, **k: {
        "decision": "review", "risk_level": "review", "suspicious_p": 0.6})
    update = msg_update("charge Marco 100 USDT")
    run(main.handle_text(update, "charge Marco 100 USDT"))
    assert store.open_invoices() == [], 'nothing may be minted here'
    assert "Held for review" in update.message.sent


def test_a_clean_verdict_mints_immediately_and_hands_back_a_pay_link(monkeypatch):
    """A tap on every clean invoice was friction on the happy path — and, measured
    live, the tap was the point where a first-time user stalled."""
    monkeypatch.setattr(jev, "parse_invoice", lambda text: dict(DRAFT, raw=text))
    monkeypatch.setattr(jev, "screen_invoice", lambda *a, **k: {
        "decision": "settle", "risk_level": "clean", "suspicious_p": 0.0,
        "duplicate_p": 0.0, "pressure_p": 0.0})
    monkeypatch.setattr(x402, "head_block", lambda: 14000)
    update = msg_update("charge Marco 100 USDT")
    run(main.handle_text(update, "charge Marco 100 USDT"))
    invs = list(store._load()["invoices"].values())
    assert len(invs) == 1, "a clean sentence must produce an invoice"
    sent = "\n".join(update.message.replies)
    assert "Minted" in sent and "/i/" in sent, "the reply must carry the pay link"
    assert invs[0]["created_block"] == 14000


def test_screen_answers_off_schema_never_settle(monkeypatch):
    """Pre-fix: missing answers or an off-vocabulary level mapped to 'clean'."""
    monkeypatch.setattr(jev, "ask", lambda *a, **k: {})
    empty = jev.screen_invoice({"amount": "1"})
    assert empty["decision"] != "settle"
    assert empty["risk_level"] == "review"
    monkeypatch.setattr(jev, "ask", lambda *a, **k: {
        "risk_level": {"score": "weird"}, "is_suspicious": {"noul": "n/a"}})
    odd = jev.screen_invoice({"amount": "1"})
    assert odd["decision"] != "settle"
    assert odd["risk_level"] == "review"


def test_clean_answers_still_settle(monkeypatch):
    monkeypatch.setattr(jev, "ask", lambda *a, **k: {
        "is_suspicious": {"noul": 0.05}, "is_duplicate": {"noul": 0.1},
        "uses_pressure": {"noul": 0.0}, "risk_level": {"score": "clean"}})
    assert jev.screen_invoice({"amount": "1"})["decision"] == "settle"


def test_amount_must_come_from_the_offered_candidates(monkeypatch):
    monkeypatch.setattr(jev, "ask", lambda *a, **k: {
        "is_invoice_intent": {"noul": 0.9},
        "pick_amount": {"choice": "999", "confidence": 0.99}})
    assert jev.parse_invoice("charge Marco 100 USDT")["amount"] is None


# ------------------------------------- 5. witness binding and consumption
def test_check_scans_from_the_creation_block(monkeypatch):
    eth = install_chain(monkeypatch, [])
    inv = mint(created_block=12345)
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    assert eth.queries and eth.queries[0]["fromBlock"] == 12345


def test_check_falls_back_to_head_window_without_a_creation_block(monkeypatch):
    eth = install_chain(monkeypatch, [], head=20000)
    inv = mint(created_block=None)
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    assert eth.queries[0]["fromBlock"] == 15000


def test_one_transfer_settles_at_most_one_invoice(monkeypatch):
    wallet = os.environ["INVOICE_WALLET"]
    tx = "bb" * 32
    install_chain(monkeypatch, [transfer_log(wallet, 100_000_000, tx=tx)])
    install_proof(monkeypatch)
    a = mint(dict(DRAFT, recipient="A"), created_block=14000)
    b = mint(dict(DRAFT, recipient="B"), created_block=14000)
    update, ctx = cmd([a["id"]])
    run(main.check(update, ctx))
    assert store.get(a["id"])["status"] == "settled"
    update, ctx = cmd([b["id"]])
    run(main.check(update, ctx))
    assert store.get(b["id"])["status"] == "open"
    assert store.tx_used(store.get(a["id"])["settlement_tx"]) == a["id"]


def install_proof(monkeypatch, ok=True, reason="verified"):
    """Inject the onchain verdict: /check asks bot.verify, not the transfer list."""
    from bot import verify
    monkeypatch.setattr(verify, "payment_proof",
                        lambda inv, tx=None, web3=None: {"ok": ok, "reason": reason,
                                                         "from": "0x" + "22" * 20})


def test_store_refuses_to_rebind_a_consumed_tx():
    a, b = mint(), mint()
    assert store.mark_settled(a["id"], "0xdead")["status"] == "settled"
    assert store.mark_settled(b["id"], "0xdead") is None
    assert store.get(b["id"])["status"] == "open"


def test_recheck_does_not_rewrite_a_settled_invoice(monkeypatch):
    wallet = os.environ["INVOICE_WALLET"]
    install_chain(monkeypatch, [transfer_log(wallet, 100_000_000, tx="cc" * 32)])
    install_proof(monkeypatch)
    inv = mint(created_block=14000)
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    first = store.get(inv["id"])["settlement_tx"]
    assert first and store.get(inv["id"])["status"] == "settled"
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    after = store.get(inv["id"])
    assert after["settlement_tx"] == first
    assert after["status"] == "settled"
    assert "cannot be settled" in update.message.sent


def test_underpaid_transfer_does_not_settle(monkeypatch):
    wallet = os.environ["INVOICE_WALLET"]
    install_chain(monkeypatch, [transfer_log(wallet, 99_999_999)])
    inv = mint(created_block=14000)
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    assert store.get(inv["id"])["status"] == "open"


def test_check_wraps_unpriceable_invoice(monkeypatch):
    install_chain(monkeypatch, [])
    inv = mint({"amount": "?", "currency": "PEN", "recipient": "A", "raw": "x"})
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    assert "Can't price that" in update.message.sent


# --------------------------------------------- 6. pending drafts are authored
def test_a_held_verdict_mints_nothing_and_leaves_a_ticket(monkeypatch):
    """The other half of the rule: a hold must not produce a payable invoice."""
    monkeypatch.setattr(jev, "parse_invoice", lambda text: dict(DRAFT, raw=text))
    monkeypatch.setattr(jev, "screen_invoice", lambda *a, **k: {
        "decision": "review", "risk_level": "review", "suspicious_p": 0.9,
        "duplicate_p": 0.1, "pressure_p": 0.1})
    update = msg_update("charge Marco 100 USDT")
    run(main.handle_text(update, "charge Marco 100 USDT"))
    invs = list(store._load()["invoices"].values())
    assert len(invs) == 1 and invs[0]["status"] == "held"
    assert "/i/" not in "\n".join(update.message.replies)
    assert store.open_invoices() == []


def test_minted_invoice_records_the_block_it_was_created_at(monkeypatch):
    """The watcher needs this: transfers older than the invoice are not its payment."""
    monkeypatch.setattr(jev, "parse_invoice", lambda text: dict(DRAFT, raw=text))
    monkeypatch.setattr(jev, "screen_invoice", lambda *a, **k: {
        "decision": "settle", "risk_level": "clean", "suspicious_p": 0.0,
        "duplicate_p": 0.0, "pressure_p": 0.0})
    monkeypatch.setattr(x402, "head_block", lambda: 123456)
    run(main.handle_text(msg_update("charge Marco 100 USDT"), "charge Marco 100 USDT"))
    inv = list(store._load()["invoices"].values())[0]
    assert inv["created_block"] == 123456


def test_a_bare_first_name_is_flagged_before_the_screen_spends_money():
    """'charge Jack …' mints, then the name screen matches and the invoice is dead
    and the fee spent. Say it at mint time instead."""
    assert "bare first name" in main._bare_name_hint("Jack")
    for ok in ("Jack Miller", "@marco", "", None, "Ana Souza"):
        assert main._bare_name_hint(ok) == ""


def test_invoice_ids_are_not_enumerable():
    a, b = mint(), mint()
    assert a["id"].startswith("INV-0001-") and len(a["id"]) == len("INV-0001-") + 6
    assert a["id"] != b["id"]
    assert store.get(a["id"].lower())["id"] == a["id"]


# --------------------------------------------- 7. machine-pay spend controls
def test_peer_cannot_choose_an_unbounded_authorization_window(monkeypatch):
    captured = {}

    def fake_sign(owner_key, token, name, version, chain_id, to_addr, value,
                  valid_after, valid_before, nonce):
        captured["valid_before"] = valid_before
        captured["valid_after"] = valid_after
        return {"v": 27, "r": "0x1", "s": "0x1", "signature": "0x00",
                "signer": "0x00", "auth": {"to": to_addr, "value": value,
                                           "validAfter": valid_after,
                                           "validBefore": valid_before,
                                           "nonce": "00" * 32}}

    monkeypatch.setattr(buy, "sign_eip3009", fake_sign)
    scheme = buy.ExactCeloScheme("0x" + "11" * 32)
    now = int(time.time())
    scheme.create_payment_payload(SimpleNamespace(
        network=buy.CELO_NETWORK, asset="0xabc", pay_to="0xdef", amount=1_000_000,
        extra={"name": "USDC", "version": "2"},
        max_timeout_seconds=10**9))  # ~31.7 years, as observed from a peer
    assert captured["valid_before"] - now <= buy.MAX_AUTH_SECONDS + 2
    assert captured["valid_after"] <= now


def test_aggregate_spend_ledger_blocks_runaway_spending(monkeypatch):
    url = "https://agent402.tools/api/paid"
    monkeypatch.setattr(buy, "MAX_TOTAL_USD", 2.0)
    monkeypatch.setattr(buy, "MAX_PER_HOST_USD", 5.0)
    for nonce in ("0x1", "0x2"):
        buy.spend_gate(url, 1_000_000)
        buy.spend_record(url, 1_000_000, nonce)
    # Total budget: two $1 authorizations already issued by the same host.
    with pytest.raises(RuntimeError, match="total budget"):
        buy.spend_gate(url, 1_000_000)
    # Per-host budget bites first when it is the tighter one.
    monkeypatch.setattr(buy, "MAX_TOTAL_USD", 100.0)
    monkeypatch.setattr(buy, "MAX_PER_HOST_USD", 1.5)
    with pytest.raises(RuntimeError, match="host budget"):
        buy.spend_gate(url, 1_000_000)
    ledger = json.loads(Path(buy.SPEND_LEDGER).read_text())
    assert ledger["total_usd"] == 2.0 and len(ledger["issued"]) == 2


def test_spend_gate_rejects_non_positive_amounts():
    with pytest.raises(RuntimeError, match="non-positive"):
        buy.spend_gate("https://agent402.tools/x", 0)


def test_hook_reads_the_offer_amount_or_signals_unknown():
    assert buy._hook_amount(
        SimpleNamespace(selected_requirements=SimpleNamespace(amount=7))) == 7
    assert buy._hook_amount(SimpleNamespace(
        payment_payload=SimpleNamespace(payload={"authorization": {"value": "9"}}))) == 9
    assert buy._hook_amount(SimpleNamespace()) is None


def test_budget_url_is_threaded_into_paid_session(monkeypatch):
    seen = {}
    if "requests" not in sys.modules:  # the sandbox image ships no requests
        stub = types.ModuleType("requests")
        stub.Session = object
        monkeypatch.setitem(sys.modules, "requests", stub)

    def fake_paid_session(owner_key=None, max_per_call_usd=1.0, budget_url=None):
        seen["url"] = budget_url
        raise RuntimeError("stop before network")

    monkeypatch.setattr(buy, "paid_session", fake_paid_session)
    with pytest.raises(RuntimeError, match="stop before network"):
        buy.pay_and_call("https://agent402.tools/api/paid")
    assert seen["url"] == "https://agent402.tools/api/paid"


def test_check_refuses_a_witness_the_chain_does_not_back(monkeypatch):
    """/check used to trust recipient+token+amount; the watcher had stopped."""
    wallet = os.environ["INVOICE_WALLET"]
    install_chain(monkeypatch, [transfer_log(wallet, 100_000_000)])
    install_proof(monkeypatch, ok=False, reason="initiated by the payout address itself")
    inv = mint(created_block=14000)
    update, ctx = cmd([inv["id"]])
    run(main.check(update, ctx))
    assert store.get(inv["id"])["status"] == "open"
    assert "does not prove payment" in update.message.sent


def test_check_refuses_a_held_invoice(monkeypatch):
    wallet = os.environ["INVOICE_WALLET"]
    install_chain(monkeypatch, [transfer_log(wallet, 100_000_000)])
    install_proof(monkeypatch)
    held = store.create(dict(DRAFT), {"decision": "review", "risk_level": "review"},
                        "user7", payout_address=wallet, status="held")
    update, ctx = cmd([held["id"]])
    run(main.check(update, ctx))
    assert store.get(held["id"])["status"] == "held"
    assert "cannot be settled" in update.message.sent
