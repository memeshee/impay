"""The hold/release path and trust explainability.

Pre-fix a "review" verdict told the user a human would release it and left that
human nothing to act on: no stored ticket, no command, no way to say yes. These
tests pin the ticket lifecycle and the two properties that make it safe — a held
invoice is never payable, and the settlement window starts at release.
"""

import pytest

from bot import jev, main, serve, store

PAY_TO = "0x86E95581E41946ED84956433a8a9c836bCbA636c"
OTHER = "0x1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", str(tmp_path / "invoices.json"))
    monkeypatch.setenv("INVOICE_WALLET", PAY_TO)
    return tmp_path


def _held(**kw):
    draft = {"amount": "500", "currency": "BRL", "recipient": "Marco Silva",
             "raw": "pay now or else"}
    screen = {"decision": "review", "suspicious_p": 0.42, "duplicate_p": 0.71,
              "pressure_p": 0.66, "risk_level": "review"}
    return store.create(draft, screen, creator="marco", payout_address=OTHER,
                        creator_chat_id=7, status="held", **kw)


def test_held_invoice_is_not_in_the_settlement_queue():
    held = _held()
    assert held["status"] == "held"
    assert held["id"] not in [i["id"] for i in store.open_invoices()]
    assert store.held_invoices()[0]["id"] == held["id"]


def test_release_moves_it_to_open_and_restarts_the_settlement_window():
    held = _held()
    released = store.release(held["id"], payout_address=None, created_block=999)
    assert released["status"] == "open"
    # Window anchored at release, not at draft time: a transfer mined while the
    # ticket sat in the queue must not become its proof.
    assert released["created_block"] == 999
    assert released["payout_address"] == OTHER          # kept, not overwritten
    assert released["id"] in [i["id"] for i in store.open_invoices()]


def test_release_of_a_non_held_invoice_is_refused():
    assert store.release("INV-9999-NOPE", payout_address=None, created_block=1) is None


def test_drop_is_terminal():
    held = _held()
    assert store.drop(held["id"])["status"] == "dropped"
    assert store.release(held["id"], payout_address=None, created_block=5) is None
    assert store.held_invoices() == []


def test_release_can_attach_a_payout_address_when_the_draft_had_none(monkeypatch):
    monkeypatch.delenv("INVOICE_WALLET", raising=False)
    draft = {"amount": "10", "currency": "USD", "recipient": "Ana"}
    held = store.create(draft, {"decision": "review"}, creator="ana", status="held")
    assert store.payout_for(held) == ""
    released = store.release(held["id"], payout_address=PAY_TO, created_block=3)
    assert store.payout_for(released) == PAY_TO


# ------------------------------------------------------------- trust reasons

def test_trust_reasons_name_the_signals_that_fired():
    reasons = jev.trust_reasons({"suspicious_p": 0.42, "duplicate_p": 0.71,
                                 "pressure_p": 0.66, "risk_level": "review"})
    assert any("duplicate" in r for r in reasons)
    assert any("pressure" in r for r in reasons)
    assert any("fraud" not in r for r in reasons)


def test_trust_reasons_explain_a_model_only_hold():
    reasons = jev.trust_reasons({"suspicious_p": 0.1, "risk_level": "review"})
    assert reasons and "human glance" in reasons[0]


def test_trust_reasons_are_quiet_when_nothing_fired():
    assert jev.trust_reasons({"suspicious_p": 0.1, "risk_level": "clean"}) == []


# --------------------------------------------------------------- payability

def test_held_invoice_page_has_no_pay_button():
    held = _held()
    html = serve.render_invoice(serve.invoice_payload(store.get(held["id"])))
    assert "Waiting on an operator" in html
    assert "celo://wallet/pay" not in html        # a payer must not be able to send
    assert "ethereum:" not in html


def test_open_invoice_page_still_has_the_pay_button():
    inv = store.create({"amount": "10", "currency": "USD", "recipient": "Ana"},
                       {"decision": "settle"}, creator="ana", payout_address=PAY_TO)
    store.set_services(inv["id"], "done")   # screening finished, so it is payable
    html = serve.render_invoice(serve.invoice_payload(store.get(inv["id"])))
    assert "celo://wallet/pay" in html and "awaiting payment" in html


def test_an_unscreened_invoice_offers_no_way_to_pay():
    """The watcher will not settle an invoice whose screening has not finished, so a
    page that shows pay controls before then invites money the agent must refuse —
    and if the screen then matches, that money is stranded."""
    inv = store.create({"amount": "10", "currency": "USD", "recipient": "Ana"},
                       {"decision": "settle"}, creator="ana", payout_address=PAY_TO)
    payload = serve.invoice_payload(store.get(inv["id"]))
    assert payload["payable"] is False and payload["screening_pending"] is True
    html = serve.render_invoice(payload)
    assert "Screening in progress" in html
    assert "celo://wallet/pay" not in html
    # no markup to pay with: no copy fields, no wallet button, no send card
    assert "data-copy=" not in html
    assert 'id="paywallet"' not in html
    assert 'id="sendbox"' not in html


def test_dropped_invoice_page_refuses():
    held = _held()
    store.drop(held["id"])
    html = serve.render_invoice(serve.invoice_payload(store.get(held["id"])))
    assert "Dropped" in html and "celo://wallet/pay" not in html


def test_a_refused_page_never_needs_a_live_rate(monkeypatch):
    """A page that refuses payment must not be priced.

    Pricing a non-USD invoice fetches an FX rate, and that fetch ran on every
    status: offline it failed, the payload carried `error`, and the page showed
    "cannot price this invoice" instead of the real status — which is how this
    test failed in a reviewer's sandbox while passing on a machine with wifi.
    """
    def explode(*_a, **_k):
        raise AssertionError("a non-payable invoice must never be priced")

    real = serve.x402.create_payment_request
    monkeypatch.setattr(serve.x402, "create_payment_request", explode)

    dropped = _held()
    store.drop(dropped["id"])
    settled = store.create({"amount": "10", "currency": "USD", "recipient": "Ana"},
                           {"decision": "settle"}, creator="ana", payout_address=PAY_TO)
    store.mark_settled(settled["id"], "0x" + "ab" * 32)
    cases = {
        "Waiting on an operator": _held()["id"],
        "Dropped": dropped["id"],
        "Payment confirmed onchain.": settled["id"],
    }
    for expected, inv_id in cases.items():
        inv = store.get(inv_id)
        assert inv, inv_id
        payload = serve.invoice_payload(inv)
        assert payload["payable"] is False
        assert "error" not in payload, payload.get("error")
        html = serve.render_invoice(payload)
        assert expected in html, (expected, inv_id)
        assert "celo://wallet/pay" not in html and "ethereum:" not in html

    # ... and an open invoice is still priced and still payable.
    monkeypatch.setattr(serve.x402, "create_payment_request", real)
    open_inv = store.create({"amount": "10", "currency": "USD", "recipient": "Ana"},
                            {"decision": "settle"}, creator="ana", payout_address=PAY_TO)
    store.set_services(open_inv["id"], "done")   # screening finished, so it is payable
    open_row = store.get(open_inv["id"])

    assert open_row
    assert serve.invoice_payload(open_row)["payable"] is True


# ------------------------------------- release: a hold is not a clearance to pay
def test_a_held_invoice_is_screened_before_any_operator_acts():
    """Held invoices used to sit unscreened until release, so the operator cleared a
    hold blind and the invoice then went blocked by a match nobody could see."""
    held = _held()
    assert held["services_status"] == "held"
    assert held["id"] in [i["id"] for i in store.invoices_awaiting_services()]
    green = {"receipts": [{"slug": "sanctions-name", "ok": True,
                           "result": {"verdict": "no_match_on_lists_checked", "clean": True}}],
             "blocked": False, "reason": None, "spent_usd": 0.005, "at": 1}
    after = store.set_services(held["id"], "done", green)
    assert after["status"] == "held"          # still an operator's call
    assert after["services_status"] == "done"  # but now the verdict is known


def test_a_sanctions_match_blocks_a_held_invoice_instead_of_leaving_it_queued():
    held = _held()
    red = {"receipts": [{"slug": "sanctions-name", "ok": True,
                         "result": {"verdict": "match", "clean": False}}],
           "blocked": True, "reason": "sanctions-name flagged: verdict=match for 'Marco Silva'",
           "spent_usd": 0.005, "at": 1}
    after = store.set_services(held["id"], "done", red)
    assert after["status"] == "blocked"       # not a ticket an operator can clear
    assert store.release(held["id"], payout_address=None, created_block=1) is None


def test_release_keeps_a_clean_screen_so_the_page_is_payable_immediately():
    """Release used to reset services_status to "pending", so an already-screened
    invoice became unpayable again ("screening in progress") and the fee was paid twice."""
    # USD, so pricing needs no FX fetch: this test is about the release, and the
    # offline suite blocks sockets by design.
    held = store.create({"amount": "10", "currency": "USD", "recipient": "Marco Silva"},
                        {"decision": "review", "suspicious_p": 0.4, "duplicate_p": 0.9,
                         "pressure_p": 0.1, "risk_level": "review"},
                        creator="marco", payout_address=OTHER, creator_chat_id=7,
                        status="held")
    green = {"receipts": [{"slug": "sanctions-name", "ok": True,
                           "result": {"verdict": "no_match_on_lists_checked", "clean": True}}],
             "blocked": False, "reason": None, "spent_usd": 0.005, "at": 1}
    store.set_services(held["id"], "done", green)
    released = store.release(held["id"], payout_address=None, created_block=1)
    assert released["status"] == "open" and released["services_status"] == "done"
    payload = serve.invoice_payload(store.get(held["id"]))
    assert payload["payable"] is True and not payload.get("screening_pending")
    assert 'id="sendbox"' in serve.render_invoice(payload)


def test_the_release_reply_does_not_promise_payment_before_the_screen_ran():
    """'can be paid now' was sent 14s before the screen blocked the invoice."""
    unscreened = {"id": "INV-1", "services_status": "pending"}
    assert "payable now" not in main._release_state(unscreened)
    assert "payable only if it comes back clean" in main._release_state(unscreened)
    screened = {"id": "INV-1", "services_status": "done"}
    assert "payable now" in main._release_state(screened)


def test_releasing_a_screen_hold_says_the_operator_accepted_the_candidates():
    """"already ran clean" was wrong for an invoice held on substring noise."""
    released = {"id": "INV-1", "services_status": "done",
                "held_reason": "sanctions-name found 8 entries containing part of 'Bao'"}
    text = main._release_state(released)
    assert "you accepted" in text and "clean" not in text
