"""The HTTP surface, driven over a real socket.

Route wiring, status codes and security headers are exactly the things that look
fine in a unit test of a render function and break the moment a judge loads the
page, so these start the actual server on an ephemeral port.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from bot import serve, store

PAY_TO = "0x86E95581E41946ED84956433a8a9c836bCbA636c"


@pytest.fixture()
def base_url(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", str(tmp_path / "invoices.json"))
    monkeypatch.setenv("INVOICE_WALLET", PAY_TO)
    monkeypatch.setattr(serve, "services_verify",
                        lambda: (lambda inv: {"ok": True, "from": "0x" + "22" * 20,
                                              "pay_to": PAY_TO, "reason": "verified"}))
    serve._PROOF_CACHE.update(at=0.0, data=None)
    server = serve.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        serve._PROOF_CACHE.update(at=0.0, data=None)


def get(url, method="GET", headers=None):
    hdrs = {"User-Agent": "test/1.0"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def _invoice(**kw):
    return store.create({"amount": "10", "currency": "USD", "recipient": "Ana"},
                        {"decision": "settle"}, creator="ana", payout_address=PAY_TO, **kw)


def test_health(base_url):
    code, headers, body = get(base_url + "/health")
    assert code == 200 and json.loads(body)["ok"] is True


def test_security_headers_are_on_every_response(base_url):
    for path in ("/health", "/", "/proof", "/api/proof"):
        _, headers, _ = get(base_url + path)
        assert "Content-Security-Policy" in headers, path
        assert headers.get("X-Content-Type-Options") == "nosniff", path


def test_index_and_proof_pages_render(base_url):
    code, _, body = get(base_url + "/")
    assert code == 200 and "impay" in body
    code, _, body = get(base_url + "/proof")
    assert code == 200 and "Onchain proof" in body


def test_proof_api_shape(base_url):
    code, headers, body = get(base_url + "/api/proof")
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    data = json.loads(body)
    assert set(data["counts"]) >= {"settled", "verified", "independent", "purchases"}


def test_invoice_page_and_api_for_a_real_invoice(base_url):
    inv = _invoice()
    code, headers, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200 and inv["id"] in body
    assert "celo://wallet/pay" in body
    code, _, body = get(f"{base_url}/api/invoice/{inv['id']}")
    data = json.loads(body)
    assert code == 200 and data["status"] == "open" and data["amount_atomic"] == 10_000_000


def test_unknown_invoice_is_a_404_not_a_stack_trace(base_url):
    code, _, body = get(base_url + "/i/INV-9999-NOPE")
    assert code == 404 and "<!doctype html>" in body.lower()
    code, _, body = get(base_url + "/api/invoice/INV-9999-NOPE")
    assert code == 404 and json.loads(body)["error"] == "unknown invoice"


def test_unknown_route_returns_json_404(base_url):
    code, _, body = get(base_url + "/definitely/not/a/route")
    assert code == 404 and json.loads(body)["error"] == "not found"


def test_head_requests_do_not_send_a_body(base_url):
    code, headers, body = get(base_url + "/health", method="HEAD")
    assert code == 200 and body == ""
    assert int(headers["Content-Length"]) > 0


def test_settled_invoice_page_shows_the_explorer_link(base_url):
    inv = _invoice()
    store.mark_settled(inv["id"], "0x" + "cd" * 32)
    code, _, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200 and "blockscout.com/tx/0xcdcd" in body


def test_agent_card_is_machine_readable(base_url):
    code, headers, body = get(base_url + "/agent.json")
    data = json.loads(body)
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    assert data["registrations"][0]["agentId"]
    assert data["registrations"][0]["agentRegistry"].startswith("eip155:42220:")
    ids = [c["id"] for c in data["capabilities"]]
    assert "invoice.proof" in ids and "settlement.proof" in ids
    assert data["settlement"]["chain"] == "eip155:42220"


def test_invoice_proof_endpoint_returns_the_verdict(base_url, monkeypatch):
    from bot import verify
    inv = _invoice()
    monkeypatch.setattr(verify, "payment_proof",
                        lambda i, tx=None, web3=None: {"ok": True, "reason": "verified",
                                                       "id": i["id"]})
    code, _, body = get(f"{base_url}/api/invoice/{inv['id']}/proof")
    assert code == 200 and json.loads(body)["ok"] is True


def test_invoice_proof_endpoint_404s_for_an_unknown_invoice(base_url):
    code, _, body = get(base_url + "/api/invoice/INV-9999-NOPE/proof")
    assert code == 404 and json.loads(body)["error"] == "unknown invoice"


@pytest.mark.parametrize("status,words", [
    ("expired", "Expired"),
    ("held", "Held for review"),
    ("dropped", "Dropped"),
    ("blocked", "Blocked by sanctions screening"),
])
def test_non_payable_invoices_render_no_pay_button(base_url, status, words):
    """A stale or held link that still takes money is the bug this prevents."""
    inv = _invoice()
    db = store._load()
    db["invoices"][inv["id"]]["status"] = status
    db["invoices"][inv["id"]]["blocked_reason"] = "test"
    store._save(db)
    code, _, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200 and words in body
    assert "celo://wallet/pay" not in body
    assert "Pay 10" not in body


def test_an_open_invoice_does_render_a_pay_button(base_url):
    inv = _invoice()
    code, _, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200 and "celo://wallet/pay" in body


@pytest.mark.parametrize("status,label", [
    ("expired", "expired"),
    ("weird_status_from_the_future", "not payable"),
])
def test_the_badge_cannot_claim_a_review_that_never_happened(base_url, status, label):
    """An expired demo link rendered the 'held for review' badge: the body said
    expired while the badge claimed a human had looked at it. Every state names
    itself, and an unknown one does not borrow a truthful state's label."""
    inv = _invoice()
    db = store._load()
    db["invoices"][inv["id"]]["status"] = status
    store._save(db)
    code, _, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200
    assert f'id="badge">{label}</span>' in body
    assert 'id="badge">held for review</span>' not in body


def test_a_desktop_payer_gets_copyable_fields_not_a_dead_deep_link(base_url):
    """A `celo://` link in a desktop browser dies with 'the scheme does not have a
    registered handler' — the teal button was the only prominent action, so the
    one obvious way to pay did nothing. The manual transfer is primary instead."""
    inv = _invoice()
    code, _, body = get(f"{base_url}/i/{inv['id']}",
                        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/140"})
    assert code == 200
    assert 'id="sendbox"' in body
    # copyable amount, token contract and address — the transfer a wallet can send
    assert f'data-copy="{PAY_TO}"' in body
    assert body.count('class="copy"') >= 2
    # the deep link survives, but never as the primary button on desktop
    assert 'class="btn ghost" href="celo://wallet/pay' in body
    assert 'class="btn" href="celo://wallet/pay' not in body
    assert "registered handler" in body  # the page says why the links may fail


def test_a_phone_still_gets_the_deep_link_as_the_primary_action(base_url):
    inv = _invoice()
    code, _, body = get(f"{base_url}/i/{inv['id']}",
                        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile"})
    assert code == 200
    assert 'class="btn" href="celo://wallet/pay' in body
    assert 'id="sendbox"' in body  # the manual path is still there


def test_transfer_calldata_is_a_real_erc20_transfer():
    """Frame it wrong and the wallet signs a transfer of the wrong amount, to the
    wrong address — so the encoding is asserted, not eyeballed."""
    usdt = "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"
    data = serve._transfer_calldata(usdt, 20000)          # 0.02 at 6 decimals
    assert data.startswith("0xa9059cbb")
    assert len(data) == 2 + 8 + 64 + 64
    assert data[10:74] == "0" * 24 + usdt[2:].lower()     # recipient, left-padded
    assert data[74:] == format(20000, "x").rjust(64, "0")  # atomic amount
    # 18-decimal token: 25 wBRL
    wbrl = "0xd76f5faf6888e24d9f04bf92a0c8b921fe4390e0"
    assert serve._transfer_calldata(wbrl, 25 * 10 ** 18).endswith(
        format(25 * 10 ** 18, "x").rjust(64, "0"))
    for bad in ("0x1234", usdt):
        with pytest.raises(ValueError):
            serve._transfer_calldata(bad, 0)


def test_the_page_offers_a_browser_wallet_but_only_reveals_it_to_one(base_url):
    """celo:// and ethereum: have no handler in a desktop browser, so the pay button
    is an injected-wallet button, shipped hidden and revealed by the provider."""
    inv = _invoice()
    code, _, body = get(f"{base_url}/i/{inv['id']}")
    assert code == 200
    assert '<div id="walletbox" hidden>' in body
    assert 'id="paywallet"' in body
    assert 'data-calldata="0xa9059cbb' in body
    assert 'data-chain="0xa4ec"' in body          # Celo mainnet, hex
    assert "eip6963:announceProvider" in body     # multi-wallet discovery
    assert "eth_sendTransaction" in body



