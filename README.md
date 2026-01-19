# In-proces-CLOB-DEX-MM-bot

Market data ingestion and paper-trading simulator for perpetual DEX markets.

## Features (WIP)
- Async websocket clients (top-of-book + trades)
- Market selection (spread / throughput metrics)
- Paper trading simulator (position/cash/PnL)
- Config via YAML + Pydantic

## Not included
- No real trading / execution
- No credentials / private keys

## Quickstart
```bash
python -m venv venv
# activate venv (Windows)
venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python run.py
