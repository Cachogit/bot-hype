import logging
from dataclasses import dataclass
from src.strategies.base import Signal

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_position_pct: float = 0.02   # max 2% of portfolio per trade
    max_daily_loss_pct: float = 0.05  # stop trading after 5% daily loss
    max_open_positions: int = 5


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.daily_loss = 0.0
        self.open_positions: list[Signal] = []

    def approve(self, signal: Signal) -> bool:
        if self.daily_loss >= self.config.max_daily_loss_pct:
            logger.warning("Daily loss limit reached — signal rejected")
            return False
        if len(self.open_positions) >= self.config.max_open_positions:
            logger.warning("Max open positions reached — signal rejected")
            return False
        return True

    def record_loss(self, pct: float):
        self.daily_loss += pct

    def reset_daily(self):
        self.daily_loss = 0.0
