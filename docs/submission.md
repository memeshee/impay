# Submission text

Paste-ready. Every claim here is checkable from a link in this file.

## Title

**impay — one sentence in, a proven payment out**

## One-liner

**impay** is an invoice agent for freelancers who get paid across borders. A freelancer types
`charge Marco 500 BRL for the logo` in Telegram; the ERC-8004 agent
prices it, buys its own sanctions screening over x402, mints a pay page anyone can
open, and marks the invoice paid **from Celo rather than from a claim**.

## What a judge should click, in order

1. **Pay an invoice** — [0.05 USDC](https://celo.uconfidant.tech/i/INV-0007-2782D9)
   or [25 wBRL with a Ripio on-ramp](https://celo.uconfidant.tech/i/INV-0008-9E74F4).
   Both are already screened; the page flips to settled by itself when the transfer
   lands.
2. **[`/proof`](https://celo.uconfidant.tech/proof)** — every settlement claim
   re-derived from Celo on render, and each payment labelled `independent payer` /
   `builder-funded` / `unverified`. It currently reads **0 independent payers**, and
   says so.
3. **[`/agent.json`](https://celo.uconfidant.tech/agent.json)** — the
   machine-readable card, matching ERC-8004 `agentId 10005` on chain.
4. **[docs/judging.md](judging.md)** — each criterion mapped to the function that
   satisfies it.

## The three things that are actually hard

**1. The invoice is paid from the chain, not from a promise.** `bot/verify.py` is the
only definition of "was this paid": the transaction must exist, succeed, *not be
initiated by the payee*, carry the right token to the right address, and match the
amount within 5%. That last rule exists because an earlier version accepted "at least
this much", and a `0.998543 USA₮` Squid-bridge delivery got credited to a `0.1 USD`
invoice for weeks. Both claims that failed this test are still listed in the README
with their transaction hashes and the reason.

**2. The agent buys the controls it does not own, per invoice.** A sanctions screen on
the payout wallet ($0.002), on the counterparty name ($0.005) and an FX cross-check
($0.002) are paid in USA₮/USDC over x402, and the verdict **decides the money** rather
than decorating it. The vendor reports more than a verdict word, so the agent reads the
one that identifies a person: an all-token match against a listed entry blocks the
invoice for good (`Viktor Bout` → `BOUT, Viktor Anatolijevitch`, SDN 8279), a listed
payout address blocks, and a `containsCount` hit is treated as substring noise — `Bao`
matches `Bilbao` and `LIU, Baoxia`, `Anna` matches `DI ANNABA, Yacine` — so those
invoices are parked as an operator decision (`/release` or `/drop`) with the entry and
its SDN id quoted in the reason. All five readings above were verified against the live
vendor. `docs/receipts/` holds the raw 402 responses, and each
purchase's settlement is re-checked onchain.

**3. LatAm money does not round-trip through the dollar.** `/wallet <addr> wBRL` and a
BRL invoice settles **500 wBRL for 500 BRL** — in kind. The pay page also carries a
prefilled Ripio on-ramp bound to the payout address, so a payer holding no stablecoin
can still pay.

## Registration fields (what the form asks for)

- **Public repo:** https://github.com/memeshee/impay (the old `celo-invoice-agent` URL redirects)
- **ERC-8004 agentId:** `10005` · registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` · owner `0xDbFe5c4d0256D2459bB216caB905999Ca41bE349`
- **Agent wallet:** `0xDbFe5c4d0256D2459bB216caB905999Ca41bE349`
- **PayTo wallet** (x402 carries no attribution tag): `0x86E95581E41946ED84956433a8a9c836bCbA636c`
- **ERC-8021 code:** `celo_b97cdbaae280` (+ our own `celo_invoiceagent`) — verified on the wire, see the attribution block at the top of the README
- **Telegram:** [@notcelordbot](https://t.me/notcelordbot)
- **Primary track:** Build with buy — the agent pays for sanctions-name, sanctions-wallet and
  fx-rate per invoice over x402, and the verdict decides settlement (an all-token name
  match blocks; substring-only noise holds for an operator)
- **Also entering:** Open Corridors / USA₮ over x402 — a stranger pays an invoice link with no
  account; and Stable Agents: LatAm (Ripio) — a BRL invoice settles in wBRL in kind, with a
  prefilled Ripio on-ramp on the pay page

## What is not done yet

Stated in the README's Limits section rather than hidden: **0 independent payers** — every
settlement so far is builder-funded and `/proof` reports that number; **no settled wFIAT
invoice** (the wBRL pay page is live and open, the transfer has not happened); the Textile
wBRL swap is a standalone leg rather than an automatic payout, FX is a keyless rate
cross-checked against a bought one rather than a firm quote, and storage is a single JSON file.

## Engineering, for the code reviewers

279 offline tests (no network, no keys, no `.env`), ruff, a 75% coverage floor
(currently 79%) and a daily workflow that re-derives the settlement claims from Celo. The
densest tests are on the paths where silence costs money: every refusal in `bot/verify.py`,
the x402 spend budgets, the screening gate, and the HTTP surface over a real socket.
`SECURITY.md` maps each control to the test that pins it.
