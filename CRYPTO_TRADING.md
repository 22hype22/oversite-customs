# Crypto trading engine

An automated Solana trading bot, driven from Discord with `/crypto` or headless
with `python -m crypto.cli`. It quotes real routes through the Jupiter
aggregator, prices every cost before deciding, gates each order through a risk
manager, and books every fill to a local SQLite ledger.

It runs in **paper mode by default** and, in live mode, boots **disarmed**.

---

## Read this before you run it live

The post that prompted this — $0.90 into $408,000 in two days — is not a
strategy you can copy, and the engine is built to show you why rather than to
pretend otherwise.

- **Fees dominate small size.** One Solana transaction at the default priority
  fee costs roughly $0.02–$0.05. On a $0.90 trade that is 400+ basis points of
  cost before slippage or price impact. There is a test that asserts exactly
  this (`test_small_size_makes_gas_dominate`). Compounding a sub-dollar balance
  is arithmetically impossible once fees are counted.
- **Real on-chain arbitrage is a latency business.** The spreads that survive
  are taken by searchers who co-locate with validators and land bundles in the
  same block. A bot polling a public RPC endpoint sees those opportunities
  after they are gone, and pays a failed-transaction fee for trying.
- **A long run of HOLD is the bot working.** The `roundtrip` strategy exists to
  answer "is there free money here right now?" — and the honest answer is
  almost always no. If you want it to trade more, the lever is `MIN_EDGE_BPS`,
  and lowering it below your true costs is how you convert a flat equity curve
  into a declining one.
- **`basis` and `momentum` are directional.** They take real market risk. They
  can lose money, including on a run of consecutive losses. Size accordingly.

Nothing here is financial advice, and none of it is guaranteed to be
profitable. Run paper mode for long enough to see a losing streak before you
consider arming live mode.

---

## Quick start (paper)

```bash
export CRYPTO_ENABLED=1
export CRYPTO_MODE=paper
export CRYPTO_STRATEGY=basis
export CRYPTO_PAIRS="SOL/USDC"
export CRYPTO_BASE_ORDER_USD=25

python -m crypto.cli selftest    # config + connectivity + mint verification
python -m crypto.cli signal      # what would it do right now, and why
python -m crypto.cli run --arm   # paper-trade until Ctrl-C
python -m crypto.cli pnl         # how it went
```

From Discord, once the bot is running with `CRYPTO_ENABLED=1`:

```
/crypto status      engine state, PnL, latest signals
/crypto signals     live evaluation with the risk verdict per pair
/crypto start       start the loop (disarmed — evaluates, does not trade)
/crypto arm         allow orders
/crypto panic       kill switch
```

## Going live

1. `pip install -r requirements-crypto.txt` (adds `solders` + `base58`).
2. Fund a **dedicated** wallet. Do not point this at a wallet holding anything
   you are not willing to lose — the key sits in the process environment.
3. Set:
   ```bash
   export CRYPTO_MODE=live
   export SOLANA_PRIVATE_KEY="<base58 secret key>"
   export SOLANA_RPC_URL="https://<your-private-rpc>"   # public RPC will rate-limit you
   ```
4. `python -m crypto.cli selftest` — in live mode this must come back clean.
   It verifies every mint against Jupiter's token list and checks the wallet
   holds enough SOL for fees. Live mode refuses to start if it does not.
5. Start disarmed, watch it for a while, then `/crypto arm`.

The engine never auto-arms. `CRYPTO_AUTOSTART=1` starts the *loop* on boot, and
it still starts disarmed.

---

## Strategies

| `CRYPTO_STRATEGY` | What it does | Honest expectation |
|---|---|---|
| `roundtrip` | Quotes A→B and B→A; takes it only if you end with more A after two sets of fees. | Fires very rarely. This is the real "arbitrage" one. |
| `basis` (default) | Compares the executable DEX price against a CEX reference and leans against the deviation, holding inventory both sides. | Mean reversion, not arbitrage. Carries directional risk while open. |
| `momentum` | EMA-cross trend following on the DEX price. | The honest baseline. No free lunch claimed. |

Every strategy reports edge **net of the cost model**: gas (base + priority
fee, converted to USD), route price impact, slippage tolerance, and — for
`basis` — the reference venue's taker fee.

## Risk controls

All enforced in `crypto/risk.py`, checked on every order:

- **arm / disarm** — orders cannot leave the process while disarmed.
- **panic / resume** — a halt is sticky; arming does *not* clear it.
- **daily loss cap** — hits the limit, trading halts for the day.
- **position cap** per pair, and a max order notional.
- **consecutive-loss cap** — a losing streak halts the engine.
- **cooldown** between trades, and a max number in flight.
- **stale-quote rejection** and a **price-impact ceiling**.
- **venue divergence guard** — a DEX price wildly off the reference is treated
  as bad data, not as an opportunity.
- **fee-aware minimum edge** — nothing trades under `CRYPTO_MIN_EDGE_BPS`.

Failed transactions are booked to the ledger as losses, because burnt gas is
real money. A bot that hides those looks profitable while draining a wallet.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CRYPTO_ENABLED` | `0` | Master switch. |
| `CRYPTO_MODE` | `paper` | `paper` or `live`. |
| `CRYPTO_STRATEGY` | `basis` | `basis`, `roundtrip`, `momentum`. |
| `CRYPTO_PAIRS` | `SOL/USDC` | Comma-separated `BASE/QUOTE`. |
| `CRYPTO_BASE_ORDER_USD` | `25` | Size per order. |
| `CRYPTO_MAX_POSITION_USD` | `250` | Per-pair position cap. |
| `CRYPTO_MAX_DAILY_LOSS_USD` | `50` | Realized loss that halts the day. |
| `CRYPTO_MIN_EDGE_BPS` | `35` | Minimum net edge to trade. |
| `CRYPTO_MAX_SLIPPAGE_BPS` | `50` | Slippage sent to the aggregator. |
| `CRYPTO_MAX_PRICE_IMPACT_BPS` | `100` | Refuse worse routes. |
| `CRYPTO_MAX_OPEN_TRADES` | `3` | Concurrency cap. |
| `CRYPTO_MAX_CONSECUTIVE_LOSSES` | `4` | Losing streak that halts. |
| `CRYPTO_TRADE_COOLDOWN_SECONDS` | `45` | Minimum gap between trades. |
| `CRYPTO_POLL_SECONDS` | `6` | Loop interval. |
| `CRYPTO_PRIORITY_FEE_LAMPORTS` | `200000` | Per-transaction priority fee. |
| `CRYPTO_CEX_TAKER_FEE_BPS` | `10` | Reference venue fee, for `basis`. |
| `CRYPTO_REFERENCE_VENUE` | `binance` | `binance`, `coinbase`, or `none`. |
| `CRYPTO_AUTOSTART` | `0` | Start the loop on boot (still disarmed). |
| `CRYPTO_ALERT_CHANNEL_ID` | — | Channel for trade/error embeds. |
| `CRYPTO_ADMIN_USER_IDS` | — | Comma-separated user ids allowed to drive it. |
| `CRYPTO_DB_PATH` | `crypto_trades.db` | Ledger location. |
| `CRYPTO_TOKENS` | — | JSON to add/override token mints. |
| `SOLANA_PRIVATE_KEY` | — | Base58 secret key. Live mode only. |
| `SOLANA_RPC_URL` | public mainnet | Use a private RPC for live trading. |

Only the **guild owner** and ids in `CRYPTO_ADMIN_USER_IDS` can use `/crypto` —
deliberately not `manage_guild` staff, who administer tickets and pricing but
have no business arming a bot that spends the owner's wallet.

---

## Layout

```
crypto/
  config.py     env-driven settings + validation (refuses unsafe combinations)
  markets.py    token registry, mint verification, pair parsing
  quotes.py     Jupiter executable quotes + CEX reference prices
  costs.py      the cost model — gas, impact, slippage, venue fees
  strategy.py   the three strategies; all edge is reported net of costs
  risk.py       the gate every order passes through
  execution.py  PaperExecutor and LiveSolanaExecutor (sign + send + confirm)
  ledger.py     SQLite fills, positions, PnL (weighted-average cost basis)
  engine.py     the loop: quote → signal → risk → execute → record → notify
  cli.py        headless entry point
crypto_commands.py   the /crypto Discord group
tests/test_crypto.py 37 tests, all offline via httpx.MockTransport
```

`python -m pytest tests/ -q` runs the suite. No network, no wallet, no money.

## Operational notes

- The ledger is local SQLite. On an ephemeral host (Heroku-style dynos,
  containers) it is wiped on restart — point `CRYPTO_DB_PATH` at persistent
  storage if you care about the history.
- Public RPC and the free aggregator tier will rate-limit a busy loop. Keep
  `CRYPTO_POLL_SECONDS` at 6 or higher unless you are paying for an endpoint.
- The engine shuts down cleanly on SIGTERM with the rest of the bot, so a
  deploy will not leave an order in flight.
