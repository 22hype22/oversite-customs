"""Configuration for the trading engine — all of it env-driven.

Defaults are deliberately timid: paper mode, small size, a wide profit floor.
Nothing here reads a private key into logs, and ``redacted()`` is what the
Discord ``/crypto config`` command prints.
"""

import os
from dataclasses import dataclass, field, replace

from .markets import Pair, Registry, parse_pairs

PAPER = "paper"
LIVE = "live"

STRATEGIES = ("basis", "roundtrip", "momentum")


def _f(name, default):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None


def _i(name, default):
    return int(_f(name, default))


def _b(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class TradingConfig:
    enabled: bool = False
    mode: str = PAPER
    strategy: str = "basis"

    pairs: list = field(default_factory=list)
    registry: Registry = field(default_factory=Registry)

    # ---- sizing / risk -------------------------------------------------
    base_order_usd: float = 25.0
    max_position_usd: float = 250.0        # per pair, absolute notional
    max_daily_loss_usd: float = 50.0       # realized; trips the kill switch
    max_open_trades: int = 3
    max_consecutive_losses: int = 4

    # ---- edge thresholds ----------------------------------------------
    min_edge_bps: float = 35.0             # required NET of every modeled cost
    max_slippage_bps: float = 50.0         # sent to the aggregator
    max_price_impact_bps: float = 100.0    # refuse routes worse than this
    max_venue_divergence_bps: float = 1500.0   # sanity: bad data guard

    # ---- costs ---------------------------------------------------------
    priority_fee_lamports: int = 200_000
    base_fee_lamports: int = 5_000
    cex_taker_fee_bps: float = 10.0        # reference venue, for basis math
    paper_fill_haircut_bps: float = 5.0    # paper fills are pessimistic on purpose

    # ---- cadence --------------------------------------------------------
    poll_seconds: float = 6.0
    trade_cooldown_seconds: float = 45.0
    quote_stale_seconds: float = 12.0

    # ---- momentum knobs -------------------------------------------------
    ema_fast: int = 12
    ema_slow: int = 48
    momentum_min_hold_seconds: float = 300.0

    # ---- venues ---------------------------------------------------------
    jupiter_quote_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_token_url: str = "https://lite-api.jup.ag/tokens/v2"
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    reference_venue: str = "binance"       # binance | coinbase | none
    binance_url: str = "https://api.binance.com"
    coinbase_url: str = "https://api.exchange.coinbase.com"

    # ---- wallet (live only) ---------------------------------------------
    private_key: str = ""                  # base58 secret key, live mode only
    wallet_pubkey: str = ""                # optional, for read-only balance checks

    # ---- plumbing --------------------------------------------------------
    db_path: str = "crypto_trades.db"
    alert_channel_id: str = ""
    admin_user_ids: list = field(default_factory=list)
    autostart: bool = False
    verify_mints: bool = True

    # ---------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.mode == LIVE

    def validate(self):
        """Raise on anything that would make the engine unsafe or nonsensical."""
        errs = []
        if self.mode not in (PAPER, LIVE):
            errs.append(f"CRYPTO_MODE must be 'paper' or 'live', got {self.mode!r}")
        if self.strategy not in STRATEGIES:
            errs.append(f"CRYPTO_STRATEGY must be one of {STRATEGIES}, got {self.strategy!r}")
        if not self.pairs:
            errs.append("CRYPTO_PAIRS is empty")
        if self.base_order_usd <= 0:
            errs.append("CRYPTO_BASE_ORDER_USD must be > 0")
        if self.max_position_usd < self.base_order_usd:
            errs.append("CRYPTO_MAX_POSITION_USD must be >= CRYPTO_BASE_ORDER_USD")
        if self.max_daily_loss_usd <= 0:
            errs.append("CRYPTO_MAX_DAILY_LOSS_USD must be > 0")
        if self.min_edge_bps < 0:
            errs.append("CRYPTO_MIN_EDGE_BPS must be >= 0")
        if self.poll_seconds < 1:
            errs.append("CRYPTO_POLL_SECONDS must be >= 1 (be kind to public endpoints)")
        if self.ema_fast >= self.ema_slow:
            errs.append("CRYPTO_EMA_FAST must be < CRYPTO_EMA_SLOW")
        if self.is_live and not self.private_key:
            errs.append("live mode needs SOLANA_PRIVATE_KEY (base58 secret key)")
        if self.is_live and self.strategy == "roundtrip" and self.min_edge_bps <= 0:
            errs.append("live roundtrip arbitrage with min_edge_bps=0 will bleed fees; raise it")
        if errs:
            raise ValueError("Invalid crypto config:\n  - " + "\n  - ".join(errs))
        return self

    def redacted(self) -> dict:
        """Safe to print in a Discord embed."""
        d = {
            "enabled": self.enabled,
            "mode": self.mode,
            "strategy": self.strategy,
            "pairs": [p.name for p in self.pairs],
            "base_order_usd": self.base_order_usd,
            "max_position_usd": self.max_position_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_open_trades": self.max_open_trades,
            "min_edge_bps": self.min_edge_bps,
            "max_slippage_bps": self.max_slippage_bps,
            "max_price_impact_bps": self.max_price_impact_bps,
            "priority_fee_lamports": self.priority_fee_lamports,
            "poll_seconds": self.poll_seconds,
            "trade_cooldown_seconds": self.trade_cooldown_seconds,
            "reference_venue": self.reference_venue,
            "solana_rpc_url": self.solana_rpc_url,
            "wallet": self.wallet_pubkey or ("set" if self.private_key else "none"),
            "private_key": "set (hidden)" if self.private_key else "not set",
            "db_path": self.db_path,
        }
        return d

    def with_overrides(self, **kw):
        return replace(self, **kw)


def load_config(env=None) -> TradingConfig:
    """Build a config from the process environment (or a dict for tests)."""
    if env is not None:
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            return load_config()
        finally:
            os.environ.clear()
            os.environ.update(old)

    registry = Registry.from_env()
    pairs_spec = os.getenv("CRYPTO_PAIRS", "SOL/USDC")
    try:
        pairs = parse_pairs(pairs_spec, registry)
    except Exception:
        if os.getenv("CRYPTO_ENABLED"):
            raise
        pairs = []

    cfg = TradingConfig(
        enabled=_b("CRYPTO_ENABLED", False),
        mode=(os.getenv("CRYPTO_MODE", PAPER) or PAPER).strip().lower(),
        strategy=(os.getenv("CRYPTO_STRATEGY", "basis") or "basis").strip().lower(),
        pairs=pairs,
        registry=registry,
        base_order_usd=_f("CRYPTO_BASE_ORDER_USD", 25),
        max_position_usd=_f("CRYPTO_MAX_POSITION_USD", 250),
        max_daily_loss_usd=_f("CRYPTO_MAX_DAILY_LOSS_USD", 50),
        max_open_trades=_i("CRYPTO_MAX_OPEN_TRADES", 3),
        max_consecutive_losses=_i("CRYPTO_MAX_CONSECUTIVE_LOSSES", 4),
        min_edge_bps=_f("CRYPTO_MIN_EDGE_BPS", 35),
        max_slippage_bps=_f("CRYPTO_MAX_SLIPPAGE_BPS", 50),
        max_price_impact_bps=_f("CRYPTO_MAX_PRICE_IMPACT_BPS", 100),
        max_venue_divergence_bps=_f("CRYPTO_MAX_VENUE_DIVERGENCE_BPS", 1500),
        priority_fee_lamports=_i("CRYPTO_PRIORITY_FEE_LAMPORTS", 200_000),
        base_fee_lamports=_i("CRYPTO_BASE_FEE_LAMPORTS", 5_000),
        cex_taker_fee_bps=_f("CRYPTO_CEX_TAKER_FEE_BPS", 10),
        paper_fill_haircut_bps=_f("CRYPTO_PAPER_HAIRCUT_BPS", 5),
        poll_seconds=_f("CRYPTO_POLL_SECONDS", 6),
        trade_cooldown_seconds=_f("CRYPTO_TRADE_COOLDOWN_SECONDS", 45),
        quote_stale_seconds=_f("CRYPTO_QUOTE_STALE_SECONDS", 12),
        ema_fast=_i("CRYPTO_EMA_FAST", 12),
        ema_slow=_i("CRYPTO_EMA_SLOW", 48),
        momentum_min_hold_seconds=_f("CRYPTO_MOMENTUM_MIN_HOLD_SECONDS", 300),
        jupiter_quote_url=os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1").rstrip("/"),
        jupiter_token_url=os.getenv("JUPITER_TOKEN_URL", "https://lite-api.jup.ag/tokens/v2").rstrip("/"),
        solana_rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").rstrip("/"),
        reference_venue=(os.getenv("CRYPTO_REFERENCE_VENUE", "binance") or "binance").strip().lower(),
        binance_url=os.getenv("BINANCE_URL", "https://api.binance.com").rstrip("/"),
        coinbase_url=os.getenv("COINBASE_URL", "https://api.exchange.coinbase.com").rstrip("/"),
        private_key=os.getenv("SOLANA_PRIVATE_KEY", "").strip(),
        wallet_pubkey=os.getenv("SOLANA_WALLET_PUBKEY", "").strip(),
        db_path=os.getenv("CRYPTO_DB_PATH", "crypto_trades.db"),
        alert_channel_id=os.getenv("CRYPTO_ALERT_CHANNEL_ID", "").strip(),
        admin_user_ids=[x.strip() for x in (os.getenv("CRYPTO_ADMIN_USER_IDS", "") or "").split(",") if x.strip()],
        autostart=_b("CRYPTO_AUTOSTART", False),
        verify_mints=_b("CRYPTO_VERIFY_MINTS", True),
    )
    return cfg
