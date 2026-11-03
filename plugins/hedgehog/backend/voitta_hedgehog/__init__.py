"""Hedgehog Camp plugin: the StockTrak trading game (app.stocktrak.com) + Hedgehog Camp rewards.

Loaded via voitta core's plugin auto-discovery. Each tool module calls
``registry.register(...)`` at import time; ``host_pattern`` is back-filled
from manifest.json. Trading tools are additionally gated on the
``plugins.hedgehog.trading_enabled`` setting (see trading.py).
"""

from voitta_hedgehog import (  # noqa: F401  — registration side-effects
    tools,
    trading,
)
