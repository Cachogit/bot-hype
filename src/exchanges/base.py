from abc import ABC, abstractmethod
from src.strategies.base import Signal


class ExchangeClient(ABC):
    def __init__(self, settings):
        self.settings = settings

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict:
        ...

    @abstractmethod
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list:
        ...

    @abstractmethod
    async def execute(self, signal: Signal) -> dict:
        ...

    @abstractmethod
    async def get_balance(self) -> dict:
        ...
