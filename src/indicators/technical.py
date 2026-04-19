import numpy as np


def sma(prices: list[float], period: int) -> float:
    return float(np.mean(prices[-period:]))


def ema(prices: list[float], period: int) -> float:
    arr = np.array(prices, dtype=float)
    k = 2 / (period + 1)
    result = arr[0]
    for price in arr[1:]:
        result = price * k + result * (1 - k)
    return float(result)


def rsi(prices: list[float], period: int = 14) -> float:
    deltas = np.diff(prices[-period - 1:])
    gains = deltas[deltas > 0].mean() or 0
    losses = -deltas[deltas < 0].mean() or 0
    if losses == 0:
        return 100.0
    rs = gains / losses
    return float(100 - 100 / (1 + rs))


def bollinger_bands(prices: list[float], period: int = 20, std_dev: float = 2.0):
    arr = np.array(prices[-period:])
    mid = arr.mean()
    std = arr.std()
    return mid - std_dev * std, mid, mid + std_dev * std
