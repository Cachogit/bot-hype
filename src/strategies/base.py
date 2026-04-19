from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Signal:
    symbol: str
    side: Side
    quantity: float
    price: float | None = None  # None = market order
    stop_loss: float | None = None
    take_profit: float | None = None


class BaseStrategy(ABC):
    def __init__(self, symbol: str):
        self.symbol = symbol

    @abstractmethod
    async def generate_signal(self) -> Signal | None:
        ...
