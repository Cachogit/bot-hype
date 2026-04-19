from dataclasses import dataclass, field
from src.risk.manager import RiskConfig


@dataclass
class Settings:
    exchange: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    poll_interval: int = 60  # seconds between strategy evaluations
    risk: RiskConfig = field(default_factory=RiskConfig)
    log_level: str = "INFO"
