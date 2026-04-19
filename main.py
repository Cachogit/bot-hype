import asyncio
import logging
import os
from dotenv import load_dotenv
from config.settings import Settings
from src.risk.manager import RiskConfig
from src.bot import TradingBot

load_dotenv()


def build_settings() -> Settings:
    return Settings(
        exchange=os.getenv("EXCHANGE", "binance"),
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        testnet=os.getenv("TESTNET", "true").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        risk=RiskConfig(),
    )


async def main():
    settings = build_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = TradingBot(settings)
    # bot.add_strategy(MyStrategy("BTC/USDT"))  # add strategies here
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
