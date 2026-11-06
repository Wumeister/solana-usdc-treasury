# solana-usdc-treasury

![tests](https://github.com/Wumeister/solana-usdc-treasury/actions/workflows/tests.yml/badge.svg)

Earn lending interest on the idle USDC of a trading bot's Solana wallet — and get it back the moment the bot needs it.

A long/flat strategy (in the market or in cash) often sits in USDC most of the time. This small Python module parks that idle USDC in two lending protocols and withdraws it again right before the next buy:

- **Jupiter Lend Earn** (USDC)
- **Kamino Earn vault** (any USDC vault, e.g. Allez USDC or Steakhouse USDC)

Splitting between two independent protocols means one protocol having problems (paused withdrawals, an outage) never blocks all of the money.

```python
from lend_treasury import Treasury

t = Treasury(keypair, rpc_url, jup_api_key, kamino_vault, buffer_usdc=20)
t.park()     # after a SELL: keep 20 USDC in the wallet, lend the rest 50/50
t.unpark()   # before a BUY: withdraw everything from both protocols
```

## How it works

Both protocols offer a REST API that returns an **unsigned transaction**. The module

1. requests the transaction (deposit / withdraw) from the protocol's API,
2. replaces its blockhash with a fresh one from **your** RPC (the API's blockhash is sometimes not yet known to public RPC nodes),
3. signs it locally with your wallet — the private key never leaves your machine,
4. sends it and waits for confirmation.

| | Jupiter Lend Earn | Kamino Earn vault |
|---|---|---|
| API | `api.jup.ag/lend/v1/earn` | `api.kamino.finance/ktx/kvault` |
| API key | yes (free, portal.jup.ag) | no |
| Withdraw all | redeem all `shares` | withdraw the exact share balance |
| One-time setup | creates the jlUSDC token account (~0.002 SOL rent, refundable) | — |

### Robustness

- **One protocol fails → the other still works.** `park()` and `unpark()` handle each protocol separately and log errors instead of stopping.
- **Retries with a new transaction** on transient errors (e.g. `BlockhashNotFound`), but never re-sends a transaction that might still land.
- **Fees are paid in SOL**: `park()` does nothing if the wallet has less than 0.01 SOL.
- **Minimum size**: deposits below `LEND_MIN_USDC` (default 10) are skipped.

### Things learned on mainnet

- The jlUSDC receipt-token account is **not** created by Jupiter's API — deposits fail with `AccountNotInitialized` until it exists. The module creates it once.
- Kamino's documented "withdraw all" amount (`u64::MAX`) is rejected with `DECIMAL_TO_LAMPORTS_OVERFLOW`; withdrawing the exact share balance works.
- Kamino's position API lags 1–2 minutes behind the chain: right after a confirmed withdrawal it can still show shares. Do not treat that as "money still parked" without waiting.

## Install

```bash
git clone https://github.com/Wumeister/solana-usdc-treasury.git
cd solana-usdc-treasury
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python tests/test_lend_treasury.py      # offline tests, no wallet or network needed
```

## Use

```bash
cp .env.example .env        # fill in your key, Jupiter API key and Kamino vault
python lend_treasury.py kamino-vaults           # list Kamino USDC vaults with APY / TVL (no key needed)
python lend_treasury.py status
python lend_treasury.py deposit jup 10          # small test first
python lend_treasury.py withdraw-all jup
python lend_treasury.py deposit kamino 10
python lend_treasury.py withdraw-all kamino
python lend_treasury.py park                    # lend idle USDC (keeps the buffer)
python lend_treasury.py unpark                  # withdraw everything
```

Settings (`.env`):

| Variable | Meaning | Default |
|---|---|---|
| `PRIVATE_KEY` | wallet key, base58 (or `SOL_KEYPAIR_FILE` with a JSON keypair) | — |
| `JUP_API_KEY` | Jupiter API key | — |
| `KAMINO_VAULT` | Kamino USDC vault address; empty = Jupiter only | — |
| `SOL_RPC_URL` | Solana RPC | public mainnet RPC |
| `LEND_SPLIT_JUP` | share that goes to Jupiter Lend | 0.5 |
| `LEND_BUFFER_USDC` | fixed USDC kept in the wallet | (uses `LEND_BUFFER_PCT` 10 %) |
| `LEND_MIN_USDC` | minimum deposit per protocol | 10 |

See [`examples/bot_integration.py`](examples/bot_integration.py) for how to hook it into a bot's buy/sell flow.

## Tests

`tests/test_lend_treasury.py` runs fully offline: fake Jupiter / Kamino / RPC responses, but real transaction building and signing (checked with signature verification). It covers the buffer and 50/50 split, the one-time token-account creation, fresh blockhashes, one protocol being down, minimum sizes, and the fixed buffer. GitHub Actions runs it on every push.

## Risks — read this

This code moves real money. Use it at your own risk and start with small amounts.

- **Smart-contract risk**: lending protocols can be exploited.
- **Liquidity risk**: withdrawals can be limited when a pool is almost fully lent out (Jupiter Lend expands its withdrawal limit gradually; Kamino pays out from idle funds and reserves).
- **Stablecoin risk**: USDC can depeg.
- **Rates float**: the yield depends on borrowing demand.
- **API changes**: both APIs are young; field names and behaviour may change.

Not financial advice. Not affiliated with Jupiter or Kamino.

## License

MIT — see [LICENSE](LICENSE).
