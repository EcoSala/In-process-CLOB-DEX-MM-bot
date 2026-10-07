# mm_bot

Paper market-maker and public market-data recorder for [Extended](https://extended.exchange) (Starknet perpetual CLOB). **It does not send orders.**

Quotes live in memory. Fills happen only when the **public tape** prints through those paper prices. That is a research approximation, not exchange matching.

## What it does

| Mode | Command | Data |
|---|---|---|
| Live paper + record | `python run.py` | Extended WS BBO + trades |
| Desktop UI | `python main.py` | same engine, Qt dashboard |
| Recorder only | `python scripts/record.py` | L2 depth 10 + trades, no quoting |
| OHLC backtest | `python scripts/backtest.py` | Binance 1m candles as a **weaker** proxy |

Default live-test markets: **BTC-USD, ETH-USD, SOL-USD, ARB-USD, FIL-USD**.

## Paper fills (live)

1. Mid from Extended top of book.
2. `make_quote`: inventory reservation skew, lagged vol-scaled half-spread, optional OFI, toxic one-sided pull, tick snap, no crossed quotes.
3. Each **new** public trade: aggressor buy fills our ask if `trade_px >= ask`; aggressor sell fills our bid if `trade_px <= bid`. Fill price is **our quote**, size `min(remaining quote, trade qty, inventory room)`. Maker fee default 0%. Inventory-limit hedges are simulated as taker at BBO (taker fee charged, not counted as edge).

Not modeled: resting on the book, queue position, cancels vs trades at a level, latency, or per-market tick size (one `tick_size` in config).

The candle backtest is even coarser (OHLC extremes, no real book). Use recorded L2+trades for any serious replay.

## Quickstart (headless)

```bash
python -m venv venv
# Windows: venv\Scripts\activate
source venv/bin/activate
pip install -r requirements-headless.txt
cp example_config.yaml config.yaml
python run.py
```

UI (Windows/desktop): `pip install -r requirements.txt` then `python main.py`.

Copy `example_config.yaml` → `config.yaml` (gitignored). Do not commit secrets. Do not set `markets_mode: all` unless you want dozens of websockets.

## Recordings

```
{recording.dir}/{market}/{book|trades|quotes|fills}/YYYYMMDD_HH.jsonl.gz
{recording.dir}/metrics/YYYYMMDD_HH.jsonl.gz
```

Logs: `logs/mm.log`, `logs/fills.log` (append + rotate).

## Azure VM

Paper + recorder under systemd: **[deploy/azure.md](deploy/azure.md)**. Use a data disk for `recording.dir`. Still no live orders.

## License

[MIT](LICENSE)
