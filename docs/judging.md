# Judging map

One page: what to look at for each criterion, so nothing here relies on you
reading the source first.

## LatAm / wFIAT

- **Is wFIAT real and used?** `/wallet <addr> wBRL` records a settlement
  preference; a BRL invoice then charges **500 wBRL for 500 BRL** in kind
  (`bot/x402.py::settle_asset_for`, `bot/store.py::set_wallet`). No FX, no dollar
  in the middle. wARS/wCOP/wPEN/wMXN/wCLP work identically.
- **Can a real person use it?** The pay page carries a prefilled Ripio on-ramp
  link bound to the invoice's payout address (`bot/ramp.py`), so a payer with no
  stablecoin buys local-currency wFIAT with a card and it lands on the invoice.
- **Autonomous?** `bot/watch.py` settles without anyone typing a command.
- **Try it:** https://celo.uconfidant.tech/i/INV-0008-9E74F4 — an open 25 wBRL
  invoice, in kind, with the Ripio on-ramp on the page

## Build with `buy`

- **Paid calls into services we do not control:** per invoice the agent buys
  `sanctions-name` ($0.005), `sanctions-wallet` ($0.002) and, for non-USD,
  `fx-rate` ($0.002) from agent402.tools over x402 (`bot/services.py`).
- **Verified, not merely attempted:** every purchase is captured to
  `docs/receipts/buy-*.json` before parsing, and its settlement is re-checked
  onchain (USDC `Transfer` from our payer) in `bot/services.py::verify_settlement`.
- **The purchases matter:** a non-clean sanctions verdict sets the invoice to
  `blocked`, which removes it from the settlement queue. This is the compliance
  layer, not a demo of paying.
- **Spend is bounded:** an aggregate + per-host budget with an
  issued-authorization ledger (`bot/buy.py`), tested in
  `tests/test_spend_controls.py`.

## Open Corridors / x402

- **Settlement is proven, not claimed:** `bot/verify.py` is the single definition
  of "was this paid" — the tx must exist, succeed, not be initiated by the payee,
  and carry a Transfer of the right token to the payout address for the right
  amount (within 5%: "at least" is not a payment). Both the watcher and `/check`
  ask it, so the automated and manual paths cannot disagree.
- **The number that matters is the honest one:** `/proof` reports verified/total
  and labels each payment by payer class from an allowlist — `independent payer`,
  `builder-funded`, or `unverified`. Today every payment is builder-funded, and
  the page says so rather than counting our own money as a user.
- **Claims that did not hold up are still in the README**, with the transaction and
  the reason they were withdrawn. That is a better signal than a clean table.
- **Attribution:** every transaction we sign carries the ERC-8021 Schema 0 suffix
  (`bot/attribution.py`) with the assigned code `celo_b97cdbaae280` first. Evidence:
  tx `0xf2ffab18…` decodes to `['celo_b97cdbaae280', 'celo_invoiceagent']`, and the
  on-chain ERC-8004 card names the same code. Our x402 *purchases* cannot carry it —
  the facilitator (`0xc077c1a9…`) broadcasts them via `transferWithAuthorization`,
  and Celo's facilitator does not write Schema 2 tags yet
  (facilitator limitation), so the PayTo wallet is published in the README.

## Agent identity

- ERC-8004 `agentId 10005` on Celo, metadata as a self-contained `data:` URI, and
  `services[]` declaring the live public endpoint
  (`scripts/set_agent_services.py`, tx `0xa9416d84…`).

## Engineering hygiene

- `python -m pytest` passes on a clean checkout: no network, no keys, no `.env`
  (249 tests, marked otherwise). CI runs it plus `ruff` and a 75% coverage floor.
- `python scripts/verify_settlements.py` re-derives settlement claims; a daily
  workflow runs it.
- `SECURITY.md` lists the controls and the test that pins each one.

## Where it is weak, stated plainly

- FX is a keyless rate cross-checked against a *bought* rate, not a firm quote.
- Textile is wired as a standalone FX swap with committed receipts
  (`docs/receipts/fx-usdt-wbrl.json`), not yet an automatic payout leg.
- Invoice storage is a single JSON file with one writer.
- Independent payers are still the gap: the rails are proven, the users are ours.
