# impay

A freelancer types one sentence. The agent prices it, screens it, mints a pay page
anyone can open, watches Celo for the money, and marks the invoice paid **from the
chain rather than from a claim**.

Built for the Celo *Agents on Open Rails* hackathon. Celo mainnet only.

<sub>Renamed from `celo-invoice-agent`; the old GitHub URL redirects. Every
transaction we sign is tagged with the ERC-8021 code this entry is scored against,
[`celo_b97cdbaae280`](https://celoscan.io/tx/0xf2ffab18d6db0c5766dd6f3479c4f3d09fa2bcef92aa4a987daacbc004c4e5b8)
(decoded from calldata, schema 0), alongside our own `celo_invoiceagent`. It is
re-proved on every daily onchain write — `python scripts/verify_attribution.py` reads
each attestation tx's calldata and prints `6/6 transactions carry celo_b97cdbaae280`,
newest [`0x690d2ce0…`](https://celoscan.io/tx/0x690d2ce053ca648620402243f0ae2ea4d3081b307b02a0890afd440c66474018).
Older transactions carry only our own code — attribution is appended to calldata and
cannot be backfilled.</sub>

[![ci](https://github.com/memeshee/impay/actions/workflows/ci.yml/badge.svg)](https://github.com/memeshee/impay/actions/workflows/ci.yml)

<img src="docs/architecture.svg" alt="One sentence becomes a proven payment: a typed judgment, a paid x402 screening leg, a payer's ERC-20 transfer on Celo, and a chain-derived settlement" width="100%">

## Use the bot

1. Open **[@notcelordbot](https://t.me/notcelordbot)** in Telegram and press **Start**.
   The command menu is there; `/help` lists every command.
2. Tell it where you get paid, once:
   `/wallet 0xYourAddress` — or `/wallet 0xYourAddress wBRL` to be paid in reais in kind.
3. Send one sentence:

   ```
   charge Marco 500 BRL for the logo, due Friday
   ```

It replies with the amount, the currencies it can settle in, and a pay link
(`/i/INV-0007-2782D9`). Send that link to whoever pays — they need no account and no
app. The invoice settles by itself when Celo proves the transfer.

| Command | What it does |
|---|---|
| `/start`, `/help` | the same guide, and a copy-pasteable example |
| `/wallet [addr] [asset]` | show or set the payout address and settlement asset |
| `/new <sentence>` | create an invoice (same as sending the sentence) |
| `/check <id>` | re-check the chain now instead of waiting for the watcher |
| `/receipt <id>` | what the agent bought for that invoice, with explorer links |
| `/held`, `/release <id>`, `/drop <id>` | operator queue for screened invoices |

Anything the bot does not recognise gets a reply pointing at `/help` — never silence.

## Try an invoice without the bot — two are open and screened

Nobody has to sign up, install anything, or talk to me. Press pay:

- **[0.05 USDC](https://celo.uconfidant.tech/i/INV-0007-2782D9)** — the token a Celo wallet is most likely to hold
- **[25 wBRL](https://celo.uconfidant.tech/i/INV-0008-9E74F4)** — the LatAm path, with a prefilled Ripio on-ramp if you hold no stablecoin at all

Both pages flip to settled by themselves once the transfer lands — verified from the
chain, no confirm button, nobody to tell.

| | |
|---|---|
| **Bot** | [@notcelordbot](https://t.me/notcelordbot) — `/wallet`, one sentence, done |
| **Onchain proof** | [celo.uconfidant.tech/proof](https://celo.uconfidant.tech/proof) — re-derives every claim from Celo (cached 60s) |
| **Agent card** | ERC-8004 `agentId 10005` on Celo · [`/agent.json`](https://celo.uconfidant.tech/agent.json) · [8004scan](https://8004scan.io/agents/celo/10005) |
| **Judging map** | [docs/judging.md](docs/judging.md) — each criterion → the code that satisfies it |

<img src="docs/invoice-page.png" alt="A live invoice page offering 0.05 USDC, with two paid sanctions checks and their receipt links" width="340">

## What one invoice costs the agent, and why

The agent owns neither a sanctions list nor an FX feed, so it **buys them per invoice**
over x402 on Celo — and those purchases decide whether money may move.

| Bought | Price | What it decides |
|---|---|---|
| `sanctions-wallet` | $0.002 | Is the payout wallet on a watchlist? |
| `sanctions-name` | $0.005 | Is the counterparty name on one? |
| `fx-rate` | $0.002 | Cross-check on non-USD pricing |

The screen answers with more than its `verdict` word, and the agent decides on the
difference. `allTokensCount > 0` means **every** token of the name appears in one
listed entry — that blocks, and is terminal (`Viktor Bout` → `BOUT, Viktor
Anatolijevitch`, SDN 8279, verified live). A `containsCount` hit means a token appeared
somewhere (`Bao` → `Bilbao` and `LIU, Baoxia`; `Anna` → `DI ANNABA, Yacine`), which is
substring noise, not identification: those invoices go to `held`, where an operator
accepts with `/release` or refuses with `/drop`, and the reason quotes the entry and its
SDN id so it can be checked against the list. A single-token name can never identify a
person, so it holds too. Only an explicit `no_match_on_lists_checked` keeps an invoice
`open` on its own. Blocking on substring noise refused every invoice for a common name and
left nothing a human could clear. Every purchase is captured to [`docs/receipts/`](docs/receipts) before it is
parsed, and its settlement is re-checked onchain afterwards.

## Why the money cannot move on a bad invoice

Each of these is enforced in code and has a test that fails without it:

- **Parsing fails closed.** Model answers are validated against the candidates we
  offered; off-schema becomes `review`, never `settle`. A held invoice renders a
  refusal and no pay button.
- **One definition of "was this paid".** `bot/verify.py` requires the transaction to
  exist, succeed, *not be initiated by the payee*, carry the right token to the right
  address, and match the amount within 5% — "at least this much" is not a payment. The
  watcher and the manual `/check` both ask it.
- **Statuses are refusals, not labels.** `mark_settled` accepts only `open`, so a
  held, blocked, dropped or expired invoice cannot be settled by any path.
- **A failed screen is not a clearance.** Screening must be `done`; a crashed purchase
  is retried, and until it succeeds the invoice waits instead of settling unscreened.
- **One transfer settles one invoice.** A consumed-witness registry blocks replay.
- **The agent cannot overspend itself.** Aggregate and per-host x402 budgets with an
  issued-authorization ledger, and the signed authorization window is clamped whatever
  a peer asks for.

## Onchain activity you can check

All real Celo mainnet transactions. `scripts/verify_settlements.py` re-derives the
settlements, and a daily CI workflow runs it so none of this can quietly rot.

The claims it checks are committed as [`claims.json`](claims.json): settled invoice id,
amount, payout address and transaction hash — public fields only, no chat ids, no user
sentences, nothing about a counterparty. The invoice store itself stays private and
gitignored, which is why the workflow reads the snapshot instead: a fresh checkout has
no store, and an audit that needs a file which is never present either crashes or checks
nothing. `--require` closes the second hole, so an empty run fails instead of passing.

```bash
python scripts/verify_settlements.py --claims claims.json --require
```

| What | Transaction |
|---|---|
| ERC-8004 identity registered (`agentId 10005`) | [`0x9c101fb0…2b70`](https://celoscan.io/tx/0x9c101fb0dcbfeba0d95c0172b22c996bec949a4d908b4daeaf486c0cd12b2b70) |
| Agent card written and then corrected five times — `services[]` added once the endpoint really answered, the false "settles over the x402 facilitator" claim removed, `x402Support` set to false, the endpoint moved to the stable domain, and finally renamed to `impay` | [`0xa9416d84…`](https://celoscan.io/tx/0xa9416d841b049e3546bab955cff3388ef2b160b9f468f28342da557bea9b1e64) · [`0xdcd801a4…`](https://celoscan.io/tx/0xdcd801a45f54957a016047b8cd9fe7e1470dc4a9113804b14a97bd5e01eadec1) · [`0xdb1a214f…`](https://celoscan.io/tx/0xdb1a214f29df4452a38fea0d45c1df2babbaba2c57633a4c550faf3b459584a1) · [`0x0374549a…`](https://celoscan.io/tx/0x0374549a1719384ef622cd5529435648e99e45f9aad3f18d59d61d1808d44b67) · [`0xabb2c715…`](https://celoscan.io/tx/0xabb2c7153682fa0ea363305d5897ae2da48ca403b08252fd931dc16e16fbc14c) · [`0xef58a6db…`](https://celoscan.io/tx/0xef58a6dbb831785e36c5d08cc6112f5ab0513438f42b7a6612c9f512e497e97f) |
| Invoice paid 0.1 USA₮ → settled autonomously, no human | [`0x0d362780…8929`](https://celoscan.io/tx/0x0d362780feaa31c632f3ead6b09d55ab9bcc53d5c5c649520d3fa668d6648929) |
| Invoice paid 0.02 USA₮ → settled autonomously | [`0x8a19fcc4…c724c4`](https://celoscan.io/tx/0x8a19fcc4139524502b52cc25748b9a40bee64384d09986b8f809690b77c724c4) |
| **Withdrawn claim:** 0.998543 USA₮ arrived via a Squid bridge for a 0.1 USD invoice; the verifier now refuses it as an amount mismatch | [`0xe5796183…1073ee`](https://celoscan.io/tx/0xe5796183431382dd8e2e543b11140497420c8bb09cc35f5a24a52e76d731073e) |
| Agent buys an FX rate — $0.002 | [`0x1e2be36e…c51b4`](https://celoscan.io/tx/0x1e2be36e4eceb26a1908ccd1decbf35d29a5f411ab5dcf00c269e7fdad0c51b4) |
| Agent buys a wallet sanctions screen — $0.002 | [`0x1f750f0e…ebad5b`](https://celoscan.io/tx/0x1f750f0e4ef3e5db04fbf3b5fbef8a67ecda043df5604ace31ffd2db72ebad5b) |
| Agent buys a name sanctions screen — $0.005 | [`0x400ef3ae…2984dd`](https://celoscan.io/tx/0x400ef3ae497217f697fb6f151002085199ea726844d7382282567e38572984dd) |
| Textile FX: 1 USDT → 5.006 wBRL, swapped **into the agent wallet** (payout leg not wired) | [`0xcac8b05d…2eec9e0`](https://celoscan.io/tx/0xcac8b05db8899954f2e5a920d842ebca448d8e5ebc2eadc6c4096e0392eec9e0) |

<img src="docs/proof-page.png" alt="The /proof page: 2 of 2 settlement claims proven onchain, 0 from an independent payer" width="340">

`/proof` is not a dashboard: it re-runs `bot/verify.payment_proof` over every settled
invoice (cached 60s, and the page says so), so the numbers are re-derived from Celo
rather than read back out of our own store. Each payment is labelled `independent
payer`, `builder-funded` or `unverified` from an allowlist of our own wallets.

**It reads 0 independent payers today.** Every settlement came from a wallet we
control. The rails work end to end; the users are still ours.

## Deployed addresses

| Thing | Celo mainnet |
|---|---|
| ERC-8004 Identity Registry | [`0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`](https://celoscan.io/address/0x8004A169FB4a3325136EB29fA0ceB6D2e539a432) |
| Agent identity | `agentId 10005`, owned by `0xDbFe5c4d0256D2459bB216caB905999Ca41bE349` |
| USA₮ · USDC · USDT | `0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771` · `0xcEBA9300f2b948710d2653dD7B07f33A8B32118C` · `0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e` |
| wBRL · wARS · wCOP (Ripio wFIAT, 18 dec) | `0xD76f5Faf6888e24D9F04Bf92a0c8B921FE4390e0` · `0x0DC4F92879B7670e5f4e4e6e3c801D229129D90D` · `0x8a1D45e102e886510e891d2Ec656a708991e2D76` |
| x402 facilitator (outbound only — we buy services; the asset table is pinned in `bot/x402.py` and re-checked by tests) | `https://api.x402.celo.org` |
| **PayTo wallet** (x402 payments carry no ERC-8021 tag yet, so it is published here) | `0x86E95581E41946ED84956433a8a9c836bCbA636c` |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # TELEGRAM_BOT_TOKEN, AGENT_WALLET_KEY, INVOICE_WALLET
python -m bot.main        # Telegram bot
python -m bot.serve       # public invoice pages on PUBLIC_PORT
python -m bot.watch       # settlement watcher + the per-invoice purchases
```

`deploy/*.service` are the systemd units this runs under (each `Restart=always`).

**Hosting, stated plainly.** The app runs on a home machine. A Cloudflare quick tunnel
gives it an origin; Vercel serves a static landing page and reverse-proxies only the
dynamic paths (`/i/*`, `/proof`, `/api/*`, `/health`) to that origin — so the URL a
judge holds stays valid even when the tunnel hostname rotates.
`scripts/tunnel_sync.sh` republishes the proxy on every rotation and
`scripts/set_agent_services.py` rewrites the ERC-8004 card to match, because an
advertised endpoint that stops answering is worse than no advertisement.

A quick tunnel also drops for a few seconds at a time, and during a reconnect it answers
`530`. So `/agent.json` is **not** proxied: `scripts/build_site.py` freezes the card into a
static file at deploy time (fetched from the live agent, falling back to building it
in-process) and Vercel serves the filesystem before it applies a rewrite — the card is the
URL written into the on-chain registration, so it is the last thing that should depend on a
tunnel. `scripts/tunnel_watchdog.sh`, on a two-minute systemd timer, probes
`localhost/health` (restarting `impay-serve` if the app died) and the public `/health`
(restarting the tunnel after two consecutive failures, which republishes the proxy). The
landing page and the agent card are therefore always up; the invoice pages and `/proof`
need the origin, and say so honestly rather than rendering an empty state.

```bash
python scripts/verify_settlements.py         # exit != 0 if any claim fails
python scripts/verify_settlements.py --fix   # withdraw the ones that do not hold
bash scripts/publish_site.sh                 # rebuild + redeploy the public host
bash scripts/tunnel_watchdog.sh              # what the timer runs every 2 minutes
```

## Tests

```bash
pip install -r requirements.txt   # runtime deps only — that is enough
pytest                            # 279 tests, offline: no network, no keys, no .env
pytest -m network                 # live facilitator, Textile, agent402, Typesafe Jev
pytest -m live                    # reads real Celo mainnet history
ruff check .                      # lint (E, F, W, I, B), from requirements-dev.txt
```

The default run passes on a clean checkout with nothing configured, and it does so with
**only the runtime requirements installed** — no dev extras, no pytest plugins, no
secrets. That is the configuration a reviewer's sandbox uses (`pip install -r
requirements.txt`, then `pytest`), so it is the configuration CI checks: the
`reviewer-path` job installs that way and runs both `pytest` and `python -m pytest`
against an empty `$HOME`.

Offline is enforced rather than asserted. `conftest.py` wraps `socket.connect`,
`connect_ex` and `getaddrinfo` in stdlib code that refuses anything outside
loopback, so an unmarked test that reaches for the network fails with
`OfflineViolation: blocked DNS lookup for 'api.x402.celo.org': this test is not marked
network` instead of passing on a laptop with wifi and hanging in a sandbox. The
wrappers are re-asserted before every test, because anything that saves and restores
`socket.connect` around a test — the `pytest-socket` plugin's teardown does — would
otherwise disarm the guard halfway through a run. `tests/test_offline_guard.py` tests
the guard itself, and a CI job runs it on a runtime-only install.

That guard has now caught three real leaks and one false promise. The leaks: a
paid-call receipt verified against a live RPC, a settle notification fired at
`api.telegram.org` from a test, and a refused invoice page that was priced (and so
needed an FX fetch) before it could say "Dropped". The false promise: the guard used
to live in `pytest.ini` as `addopts = --disable-socket`, which needs the
`pytest-socket` plugin from `requirements-dev.txt`; on any install without it, pytest
refused to start at all — `error: unrecognized arguments: --disable-socket` — and the
whole suite looked broken while CI, which installs dev requirements, stayed green.

CI also runs ruff, byte-compilation, a 75% coverage floor (currently 79%) and an
advisory dependency audit. The densest tests are where silence costs money — every
refusal path in `bot/verify.py`, the x402 spend budgets, the screening gate, and the
public HTTP surface over a real socket.

## Tracks this targets

- **Stable Agents: LatAm** — `/wallet <addr> wBRL` and a BRL invoice settles **500 wBRL
  for 500 BRL**: in kind, no FX, no dollar in the middle. The pay page carries a
  prefilled Ripio on-ramp bound to the payout address, so a payer who has never held a
  stablecoin can still pay.
- **Open Corridors: USA₮ over x402** — settlement is proven per transaction from the
  chain, and the asset is chosen per freelancer rather than globally. **Every payment so
  far is builder-funded**; an outside payer is the remaining gap, and `/proof` says so
  on the page.
- **Build with `buy`** — every invoice costs the agent ~$0.009 in services it does not
  own; an exact sanctions hit blocks settlement, and a fuzzy name match parks the invoice
  for an operator rather than decorating it.

## Limits (stated, not hidden)

- Non-USD fiat is priced by a keyless rate API and *cross-checked* against a rate the
  agent buys; it is not a firm market quote.
- Textile is wired as a standalone FX swap with committed receipts, not yet as an
  automatic payout leg.
- Invoice storage is a JSON file with one writer. Fine at demo scale, not multi-tenant
  safe.
- x402 payments carry no ERC-8021 tag (facilitator limitation) — hence the published
  PayTo wallet. Everything else we send is tagged.
- The sanctions screen is a screen: the vendor's own receipt calls its verdict "not a
  clearance".
- Two claims in this repo did not survive its own verifier; both are still listed above
  with the transaction and the reason. That is what the verifier is for.
