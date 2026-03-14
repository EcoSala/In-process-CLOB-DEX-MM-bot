from pydantic import BaseModel
import yaml

class AppConfig(BaseModel):
    name: str = "mm-bot"
    log_level: str = "INFO"
    tick_seconds: float = 1.0
    stats_log_every: int = 10

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
    markets_mode: str = "static"  # "static" or "all"
    markets: list[str] = []  # Used only if markets_mode == "static"
    selector: MarketSelectorConfig
    pinned_market: str | None = None  # Optional: force trading only this market
    api_base_url: str = "https://api.starknet.extended.exchange"  # For market discovery

class InventoryConfig(BaseModel):
    """Tunable parameters for the inventory-control / quote-skew pipeline."""
    # Price skew: how many bps to shift reservation price per unit of normalised inventory.
    # At inv_skew_strength=2 and norm_inv=1 (fully long), both quotes shift down ~2 bps.
    inv_skew_strength: float = 2.0

    # Size skew: fractional change in quote size per unit of normalised inventory.
    # At size_skew_strength=0.5 and norm_inv=1, bid_size is halved and ask_size is 1.5x.
    size_skew_strength: float = 0.5

    # Size multiplier clamps – prevent quotes from vanishing or becoming unbounded.
    min_size_mult: float = 0.1   # never quote less than 10 % of base size
    max_size_mult: float = 2.0   # never quote more than 200 % of base size

    # Near-limit dampening: kick in when abs(norm_inv) exceeds this threshold.
    near_limit_threshold: float = 0.8

    # Multiplier applied to the aggravating-side size when near the inventory limit.
    near_limit_side_mult: float = 0.1   # reduce bad-side size to 10 % near limit


class OFIConfig(BaseModel):
    """Order Flow Imbalance signal configuration."""
    enabled: bool = True
    mode: str = "trade"             # "trade" | "quote"
    window_seconds: float = 10.0    # look-back window for OFI calculation
    ofi_skew_strength: float = 1.0  # bps shift on reservation_price per unit OFI signal


class HedgeConfig(BaseModel):
    """Hard inventory hedge (aggressive market-order) configuration."""
    enabled: bool = True
    trigger_threshold: float = 0.9   # |norm_inv| above which hedge fires
    hedge_fraction: float = 0.5      # fraction of position to flatten per inventory-limit hedge
    cooldown_ticks: int = 5          # minimum ticks between successive hedges on same market
    # Profit-take trigger: flatten 100% if price moved this % in the favorable direction
    # (price UP when long, price DOWN when short), measured from average entry price.
    # Bypasses cooldown and norm_inv threshold.  Set to 0 to disable.
    price_move_trigger_pct: float = 1.0
    # Taker fee charged on every hedge market order (in %, e.g. 0.0225 = 0.0225%).
    # Deducted from cash on each hedge fill.
    taker_fee_pct: float = 0.0225


class AudioConfig(BaseModel):
    """Audio feedback configuration."""
    enabled: bool = True
    volume: float = 0.6   # master volume [0.0 – 1.0]


class SimConfig(BaseModel):
    enabled: bool = True
    quote_half_spread_bps: float
    quote_size_usd: float
    max_inventory_usd: float
    tick_size: float = 0.01     # exchange minimum price increment
    inventory: InventoryConfig = InventoryConfig()
    ofi: OFIConfig = OFIConfig()
    hedge: HedgeConfig = HedgeConfig()


class Config(BaseModel):
    app: AppConfig = AppConfig()
    risk: RiskConfig
    venues: VenuesConfig = VenuesConfig()
    extended_ws: ExtendedWSConfig = ExtendedWSConfig()
    audio: AudioConfig = AudioConfig()

    extended: ExtendedMarketsConfig
    sim: SimConfig


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config(**data)

