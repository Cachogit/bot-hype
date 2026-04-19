"""
Downloads HYPE/USDT:USDT 1H OHLCV data from Hyperliquid via ccxt
and saves it to data/hype_1h.csv
"""
import ccxt
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone

SYMBOL = "HYPE/USDC:USDC"
TIMEFRAME = "1h"
OUTPUT = Path(__file__).parent / "hype_1h.csv"

# Hyperliquid lists HYPE since late Nov 2024 — fetch from launch
SINCE_DATE = "2024-11-01 00:00:00"
BATCH = 500  # candles per request


def fetch_all(exchange: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int) -> list:
    all_candles = []
    print(f"Fetching {symbol} {timeframe} from {datetime.fromtimestamp(since_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')} ...")
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=BATCH)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts = batch[-1][0]
        since_ms = last_ts + exchange.parse_timeframe(timeframe) * 1000
        print(f"  fetched {len(all_candles)} candles, last: {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        if len(batch) < BATCH:
            break
        time.sleep(exchange.rateLimit / 1000)
    return all_candles


def main():
    exchange = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
    exchange.load_markets()

    since_ms = exchange.parse8601(SINCE_DATE)
    candles = fetch_all(exchange, SYMBOL, TIMEFRAME, since_ms)

    if not candles:
        print("No data received.")
        return

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("datetime").drop(columns=["timestamp"])
    df = df.sort_index().drop_duplicates()

    df.to_csv(OUTPUT)
    print(f"\nSaved {len(df)} candles to {OUTPUT}")
    print(df.tail(3).to_string())


if __name__ == "__main__":
    main()
