from pydantic import BaseModel
import yaml

class AppConfig(BaseModel):
    name: str = "mm-bot"
    log_level: str = "INFO"
    tick_seconds: float = 1.0

class RiskConfig(BaseModel):
    max_total_notional: float
    daily_loss_limit: float
    max_inventory_notional: float

class VenueConfig(BaseModel):
    enabled: bool = True

class VenuesConfig(BaseModel):
    extended: VenueConfig = VenueConfig()
    nado: VenueConfig = VenueConfig()

class ExtendedWSConfig(BaseModel):
    host: str = "wss://api.starknet.extended.exchange"
    market: str = "BTC-USD"
    depth: int = 1
    user_agent: str = "mm-bot/0.1"

class MarketSelectorConfig(BaseModel):
    min_spread_bps: float
    min_tpm: float
    top_n: int

class ExtendedMarketsConfig(BaseModel):
    markets: list[str]
    selector: MarketSelectorConfig

class SimConfig(BaseModel):
    enabled: bool = True
    quote_half_spread_bps: float
    quote_size_usd: float
    max_inventory_usd: float


class Config(BaseModel):
    app: AppConfig = AppConfig()
    risk: RiskConfig
    venues: VenuesConfig = VenuesConfig()
    extended_ws: ExtendedWSConfig = ExtendedWSConfig()

    extended: ExtendedMarketsConfig
    sim: SimConfig


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config(**data)

