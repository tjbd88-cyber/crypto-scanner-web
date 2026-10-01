from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Symbol:
    exchange: str
    pair: str
    base: str
    market: str


@dataclass(frozen=True)
class Ticker:
    pair: str
    price: float
    change_pct: float
    volume_24h: float


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
