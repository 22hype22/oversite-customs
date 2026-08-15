"""Automated crypto trading for the Oversite bot.

The package is self-contained: nothing here imports discord, so the engine can
run headless (``python -m crypto.cli run``) or be driven by the Discord bot.

Safety model, in short:
  * The engine boots in PAPER mode unless CRYPTO_MODE=live.
  * Even in live mode it boots DISARMED — no order can leave the process until
    somebody arms it (``/crypto arm`` or ``--arm``).
  * Every candidate trade must clear the cost model (fees + priority fee +
    price impact) AND the risk manager before it reaches an executor.
"""

from .config import TradingConfig, load_config
from .engine import TradingEngine
from .ledger import Ledger

__all__ = ["TradingConfig", "load_config", "TradingEngine", "Ledger"]
