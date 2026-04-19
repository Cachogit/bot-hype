import asyncio
import logging
from config.settings import Settings
from src.exchanges.base import ExchangeClient
from src.strategies.base import BaseStrategy
from src.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exchange = ExchangeClient(settings)
        self.risk = RiskManager(settings.risk)
        self.strategies: list[BaseStrategy] = []
        self.running = False

    def add_strategy(self, strategy: BaseStrategy):
        self.strategies.append(strategy)

    async def run(self):
        logger.info("Bot started")
        self.running = True
        while self.running:
            try:
                for strategy in self.strategies:
                    signal = await strategy.generate_signal()
                    if signal and self.risk.approve(signal):
                        await self.exchange.execute(signal)
                await asyncio.sleep(self.settings.poll_interval)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(5)

    def stop(self):
        self.running = False
        logger.info("Bot stopped")
