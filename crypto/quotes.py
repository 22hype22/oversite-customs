"""Market data.

Two kinds of price live here and they are not interchangeable:

  * ``DexQuote`` — an *executable* quote from the Jupiter aggregator for a
    specific size. Its price already includes route price impact, which is why
    the strategies quote the size they intend to trade rather than pricing off
    a mid and hoping.
  * ``RefPrice`` — an indicative top-of-book from a centralized venue. Used as
    a sanity reference and as the other leg of the basis strategy. You cannot
    execute against it from this process.
"""

import asyncio
import time
from dataclasses import dataclass, field

import httpx


class QuoteError(RuntimeError):
    pass


@dataclass
class DexQuote:
    input_mint: str
    output_mint: str
    in_atoms: int
    out_atoms: int
    price_impact_bps: float
    slippage_bps: float
    min_out_atoms: int          # otherAmountThreshold — worst allowed fill
    raw: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.ts

    def price(self, in_decimals: int, out_decimals: int) -> float:
        """Output units per 1 input unit, at this size."""
        i = self.in_atoms / (10 ** in_decimals)
        o = self.out_atoms / (10 ** out_decimals)
        if i <= 0:
            raise QuoteError("quote has zero input amount")
        return o / i


@dataclass
class RefPrice:
    venue: str
    symbol: str
    bid: float
    ask: float
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def age(self) -> float:
        return time.time() - self.ts

    @property
    def spread_bps(self) -> float:
        return ((self.ask - self.bid) / self.mid) * 10_000 if self.mid else 0.0


async def _get_json(http, url, params=None, timeout=15, attempts=3):
    """GET with bounded retries. Retries transport errors and 5xx/429 only —
    a 400 from the aggregator means the request is wrong and will stay wrong."""
    delay = 0.5
    last = None
    for i in range(attempts):
        try:
            r = await http.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = QuoteError(f"{url} -> HTTP {r.status_code}: {r.text[:200]}")
            else:
                raise QuoteError(f"{url} -> HTTP {r.status_code}: {r.text[:300]}")
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = QuoteError(f"{url} -> transport error: {e!r}")
        if i < attempts - 1:
            await asyncio.sleep(delay)
            delay *= 2
    raise last or QuoteError(f"{url} failed")


class JupiterQuotes:
    """Solana DEX aggregator quotes (the executable side)."""

    def __init__(self, http, base_url="https://lite-api.jup.ag/swap/v1"):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.last_error = ""
        self.ok_count = 0
        self.err_count = 0

    async def quote(self, input_mint, output_mint, amount_atoms, slippage_bps=50, only_direct=False):
        if amount_atoms <= 0:
            raise QuoteError("amount_atoms must be > 0")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_atoms)),
            "slippageBps": str(int(slippage_bps)),
        }
        if only_direct:
            params["onlyDirectRoutes"] = "true"
        try:
            data = await _get_json(self.http, f"{self.base_url}/quote", params=params)
        except QuoteError as e:
            self.err_count += 1
            self.last_error = str(e)
            raise
        if not isinstance(data, dict) or "outAmount" not in data:
            self.err_count += 1
            self.last_error = f"unexpected quote shape: {str(data)[:200]}"
            raise QuoteError(self.last_error)
        try:
            impact_pct = float(data.get("priceImpactPct") or 0.0)
        except (TypeError, ValueError):
            impact_pct = 0.0
        out_atoms = int(data["outAmount"])
        self.ok_count += 1
        self.last_error = ""
        return DexQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_atoms=int(data.get("inAmount") or amount_atoms),
            out_atoms=out_atoms,
            price_impact_bps=abs(impact_pct) * 10_000,
            slippage_bps=float(data.get("slippageBps") or slippage_bps),
            min_out_atoms=int(data.get("otherAmountThreshold") or out_atoms),
            raw=data,
        )


class BinanceRef:
    def __init__(self, http, base_url="https://api.binance.com"):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.last_error = ""

    async def price(self, symbol):
        if not symbol:
            return None
        try:
            d = await _get_json(self.http, f"{self.base_url}/api/v3/ticker/bookTicker", {"symbol": symbol})
            return RefPrice("binance", symbol, float(d["bidPrice"]), float(d["askPrice"]))
        except Exception as e:
            self.last_error = repr(e)
            return None


class CoinbaseRef:
    def __init__(self, http, base_url="https://api.exchange.coinbase.com"):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.last_error = ""

    async def price(self, product_id):
        if not product_id:
            return None
        try:
            d = await _get_json(self.http, f"{self.base_url}/products/{product_id}/ticker")
            return RefPrice("coinbase", product_id, float(d["bid"]), float(d["ask"]))
        except Exception as e:
            self.last_error = repr(e)
            return None


class NullRef:
    last_error = ""

    async def price(self, _symbol):
        return None


def build_reference(cfg, http):
    venue = (cfg.reference_venue or "none").lower()
    if venue == "binance":
        return BinanceRef(http, cfg.binance_url)
    if venue == "coinbase":
        return CoinbaseRef(http, cfg.coinbase_url)
    return NullRef()


def ref_symbol_for(cfg, token):
    venue = (cfg.reference_venue or "none").lower()
    if venue == "binance":
        return token.cex_symbol
    if venue == "coinbase":
        return token.coinbase_id
    return ""


class MarketData:
    """Thin facade the strategies talk to, so they never touch HTTP directly."""

    def __init__(self, cfg, http):
        self.cfg = cfg
        self.http = http
        self.dex = JupiterQuotes(http, cfg.jupiter_quote_url)
        self.ref = build_reference(cfg, http)

    async def dex_quote(self, in_token, out_token, in_amount, slippage_bps=None):
        return await self.dex.quote(
            in_token.mint,
            out_token.mint,
            in_token.to_atoms(in_amount),
            int(slippage_bps if slippage_bps is not None else self.cfg.max_slippage_bps),
        )

    async def reference(self, token):
        return await self.ref.price(ref_symbol_for(self.cfg, token))

    async def sol_usd(self):
        """SOL price in USD — needed to price gas in dollars."""
        sol = self.cfg.registry.get("SOL")
        ref = await self.reference(sol)
        if ref:
            return ref.mid
        # Fall back to the DEX itself: 1 SOL -> USDC.
        usdc = self.cfg.registry.get("USDC")
        q = await self.dex_quote(sol, usdc, 1.0, slippage_bps=50)
        return q.price(sol.decimals, usdc.decimals)
