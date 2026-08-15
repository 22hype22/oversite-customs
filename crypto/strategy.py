"""Strategies.

Three are shipped, and it is worth being blunt about what each can and cannot
do, because the difference is where people lose money:

``roundtrip``
    Quote A->B and B->A back to back and take the trade only if you end with
    more of A than you started with, after two sets of fees. This is the
    closest honest version of "on-chain arbitrage". It is genuinely atomic-ish
    and needs no inventory anywhere else. It will also almost never fire: the
    aggregator already routes around these, and the ones that survive are taken
    by searchers who land transactions in the same block via Jito bundles. A
    long run of HOLD here is the strategy working correctly, not a bug.

``basis``
    Compare the executable DEX price against a centralized reference and lean
    against the deviation, holding inventory on both sides. This is not
    arbitrage — it is mean reversion on the basis, and it carries real
    directional risk while the position is open. It fires often enough to be
    worth paper-trading.

``momentum``
    Plain EMA-cross trend following on the DEX price. No pretense of a free
    lunch; included because it is the honest baseline every "arb bot" should be
    compared against.

Every strategy returns edge that is already NET of the cost model.
"""

import time
from dataclasses import dataclass, field

from .costs import CostModel
from .markets import STABLES

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


@dataclass
class Signal:
    action: str                 # BUY | SELL | HOLD
    pair: object
    reason: str
    edge_bps: float = 0.0
    gross_bps: float = 0.0
    cost_bps: float = 0.0
    notional_usd: float = 0.0
    in_token: object = None
    out_token: object = None
    in_amount: float = 0.0
    quote: object = None
    ref: object = None
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def actionable(self) -> bool:
        return self.action in (BUY, SELL)

    def summary(self) -> str:
        if not self.actionable:
            return f"{self.pair} HOLD — {self.reason}"
        return (
            f"{self.pair} {self.action} ${self.notional_usd:,.2f} | "
            f"edge {self.edge_bps:+.1f}bps (gross {self.gross_bps:+.1f} - cost {self.cost_bps:.1f}) | {self.reason}"
        )


def hold(pair, reason, **kw):
    return Signal(action=HOLD, pair=pair, reason=reason, **kw)


class BaseStrategy:
    name = "base"

    def __init__(self, cfg, market, ledger):
        self.cfg = cfg
        self.market = market
        self.ledger = ledger
        self.costs = CostModel(cfg)

    async def evaluate(self, pair, sol_usd) -> Signal:
        raise NotImplementedError

    def _usd_per_unit(self, token, sol_usd):
        """USD value of one unit of `token`, or 0 when we cannot price it.

        Only stables and SOL can be priced without another quote, so a pair
        quoted in anything else is refused rather than guessed at — guessing
        here would mis-size every order on that pair.
        """
        sym = token.symbol.upper()
        if sym in STABLES:
            return 1.0
        if sym == "SOL":
            return sol_usd
        return 0.0

    def _size_in_quote(self, quote_tok, sol_usd, notional_usd):
        px = self._usd_per_unit(quote_tok, sol_usd)
        if px <= 0:
            return 0.0
        return notional_usd / px


class RoundTripStrategy(BaseStrategy):
    """A -> B -> A. Take it only if you end up with more A, net of both legs."""

    name = "roundtrip"

    async def evaluate(self, pair, sol_usd) -> Signal:
        quote_tok, base_tok = pair.quote, pair.base
        notional = self.cfg.base_order_usd
        in_amount = self._size_in_quote(quote_tok, sol_usd, notional)
        if in_amount <= 0:
            return hold(pair, f"cannot price {quote_tok.symbol} in USD for sizing")

        leg1 = await self.market.dex_quote(quote_tok, base_tok, in_amount)
        got_base = base_tok.from_atoms(leg1.out_atoms)
        if got_base <= 0:
            return hold(pair, "first leg quoted zero output")
        leg2 = await self.market.dex_quote(base_tok, quote_tok, got_base)
        back = quote_tok.from_atoms(leg2.out_atoms)

        gross_bps = ((back - in_amount) / in_amount) * 10_000
        impact = leg1.price_impact_bps + leg2.price_impact_bps
        cost = self.costs.for_round_trip(notional, sol_usd, impact)
        # price impact is already inside the quoted amounts, so do not subtract
        # it twice — only the fees the quotes do not know about.
        net_bps = gross_bps - cost.gas_bps - cost.slippage_bps

        meta = {
            "leg1_out": got_base,
            "leg2_out": back,
            "impact_bps": impact,
            "costs": cost.describe(),
        }
        if impact > self.cfg.max_price_impact_bps:
            return hold(pair, f"route impact {impact:.0f}bps over limit", meta=meta,
                        gross_bps=gross_bps, cost_bps=cost.total_bps)
        if net_bps < self.cfg.min_edge_bps:
            return hold(
                pair,
                f"round trip nets {net_bps:+.1f}bps, floor is {self.cfg.min_edge_bps:.0f}bps",
                gross_bps=gross_bps, cost_bps=cost.gas_bps + cost.slippage_bps, meta=meta,
            )
        return Signal(
            action=BUY, pair=pair,
            reason=f"round trip {quote_tok.symbol}->{base_tok.symbol}->{quote_tok.symbol} nets {net_bps:+.1f}bps",
            edge_bps=net_bps, gross_bps=gross_bps, cost_bps=cost.gas_bps + cost.slippage_bps,
            notional_usd=notional, in_token=quote_tok, out_token=base_tok,
            in_amount=in_amount, quote=leg1, meta={**meta, "roundtrip": True},
        )


class BasisStrategy(BaseStrategy):
    """Lean against DEX-vs-CEX deviation, with an inventory target."""

    name = "basis"

    async def evaluate(self, pair, sol_usd) -> Signal:
        base_tok, quote_tok = pair.base, pair.quote
        ref = await self.market.reference(base_tok)
        if not ref:
            return hold(pair, f"no reference price for {base_tok.symbol} on {self.cfg.reference_venue}")
        if ref.age > self.cfg.quote_stale_seconds:
            return hold(pair, f"reference price is {ref.age:.0f}s stale")

        notional = self.cfg.base_order_usd
        # Executable DEX prices for the size we would actually trade.
        quote_usd = self._usd_per_unit(quote_tok, sol_usd)
        buy_in = self._size_in_quote(quote_tok, sol_usd, notional)
        if buy_in <= 0:
            return hold(pair, f"cannot size in {quote_tok.symbol}")
        buy_q = await self.market.dex_quote(quote_tok, base_tok, buy_in)
        dex_ask = buy_in / base_tok.from_atoms(buy_q.out_atoms) if buy_q.out_atoms else 0.0
        if dex_ask <= 0:
            return hold(pair, "DEX buy quote returned zero")

        base_size = notional / ref.mid if ref.mid else 0.0
        sell_q = await self.market.dex_quote(base_tok, quote_tok, base_size) if base_size > 0 else None
        dex_bid = (quote_tok.from_atoms(sell_q.out_atoms) / base_size) if sell_q and base_size else 0.0

        # DEX prices are in quote-token units; convert to USD so they are
        # comparable with the reference venue.
        dex_ask *= quote_usd
        dex_bid *= quote_usd

        divergence_bps = abs((dex_ask - ref.mid) / ref.mid) * 10_000
        if divergence_bps > self.cfg.max_venue_divergence_bps:
            return hold(pair, f"venues disagree by {divergence_bps:.0f}bps — treating as bad data")

        pos = self.ledger.position(pair.name)
        target_units = (self.cfg.max_position_usd / 2) / ref.mid if ref.mid else 0.0
        held = pos.quantity
        held_usd = held * ref.mid

        # DEX cheaper than the reference -> buy the base on-chain.
        buy_gross_bps = ((ref.bid - dex_ask) / ref.mid) * 10_000
        sell_gross_bps = ((dex_bid - ref.ask) / ref.mid) * 10_000

        buy_cost = self.costs.for_basis(notional, sol_usd, buy_q.price_impact_bps)
        buy_net = buy_gross_bps - buy_cost.gas_bps - buy_cost.venue_fee_bps
        sell_cost = self.costs.for_basis(notional, sol_usd, sell_q.price_impact_bps if sell_q else 0)
        sell_net = sell_gross_bps - sell_cost.gas_bps - sell_cost.venue_fee_bps

        meta = {
            "ref_mid": ref.mid, "ref_venue": ref.venue,
            "dex_ask": dex_ask, "dex_bid": dex_bid,
            "held": held, "held_usd": held_usd, "target_units": target_units,
            "buy_net_bps": buy_net, "sell_net_bps": sell_net,
        }

        if buy_net >= self.cfg.min_edge_bps and buy_net >= sell_net:
            if held_usd + notional > self.cfg.max_position_usd:
                return hold(pair, f"buy edge {buy_net:+.1f}bps but position cap reached "
                                  f"(${held_usd:,.2f} of ${self.cfg.max_position_usd:,.2f})", meta=meta)
            if buy_q.price_impact_bps > self.cfg.max_price_impact_bps:
                return hold(pair, f"buy impact {buy_q.price_impact_bps:.0f}bps over limit", meta=meta)
            return Signal(
                action=BUY, pair=pair,
                reason=f"DEX ${dex_ask:,.4f} under {ref.venue} ${ref.bid:,.4f}",
                edge_bps=buy_net, gross_bps=buy_gross_bps,
                cost_bps=buy_cost.gas_bps + buy_cost.venue_fee_bps,
                notional_usd=notional, in_token=quote_tok, out_token=base_tok,
                in_amount=buy_in, quote=buy_q, ref=ref, meta=meta,
            )

        if sell_net >= self.cfg.min_edge_bps and sell_q:
            if held <= 0:
                return hold(pair, f"sell edge {sell_net:+.1f}bps but no {base_tok.symbol} held", meta=meta)
            size = min(base_size, held)
            if sell_q.price_impact_bps > self.cfg.max_price_impact_bps:
                return hold(pair, f"sell impact {sell_q.price_impact_bps:.0f}bps over limit", meta=meta)
            return Signal(
                action=SELL, pair=pair,
                reason=f"DEX ${dex_bid:,.4f} over {ref.venue} ${ref.ask:,.4f}",
                edge_bps=sell_net, gross_bps=sell_gross_bps,
                cost_bps=sell_cost.gas_bps + sell_cost.venue_fee_bps,
                notional_usd=size * ref.mid, in_token=base_tok, out_token=quote_tok,
                in_amount=size, quote=sell_q, ref=ref, meta=meta,
            )

        best = max(buy_net, sell_net)
        return hold(pair, f"best net edge {best:+.1f}bps under {self.cfg.min_edge_bps:.0f}bps floor", meta=meta)


class MomentumStrategy(BaseStrategy):
    """EMA cross on the executable DEX price. Trend following, no illusions."""

    name = "momentum"

    def __init__(self, cfg, market, ledger):
        super().__init__(cfg, market, ledger)
        self._ema = {}      # pair -> [fast, slow]
        self._last_action = {}

    def _update_ema(self, key, price):
        fast_k = 2 / (self.cfg.ema_fast + 1)
        slow_k = 2 / (self.cfg.ema_slow + 1)
        cur = self._ema.get(key)
        if cur is None:
            self._ema[key] = [price, price, 1]
            return None, None, 1
        f, s, n = cur
        f = price * fast_k + f * (1 - fast_k)
        s = price * slow_k + s * (1 - slow_k)
        n += 1
        self._ema[key] = [f, s, n]
        return f, s, n

    async def evaluate(self, pair, sol_usd) -> Signal:
        base_tok, quote_tok = pair.base, pair.quote
        notional = self.cfg.base_order_usd
        quote_usd = self._usd_per_unit(quote_tok, sol_usd)
        buy_in = self._size_in_quote(quote_tok, sol_usd, notional)
        if buy_in <= 0:
            return hold(pair, f"cannot size in {quote_tok.symbol}")
        q = await self.market.dex_quote(quote_tok, base_tok, buy_in)
        got = base_tok.from_atoms(q.out_atoms)
        if got <= 0:
            return hold(pair, "quote returned zero output")
        price = (buy_in / got) * quote_usd

        fast, slow, n = self._update_ema(pair.name, price)
        warmup = self.cfg.ema_slow
        if fast is None or n < warmup:
            return hold(pair, f"warming up EMAs ({n}/{warmup} samples)",
                        meta={"price": price})

        spread_bps = ((fast - slow) / slow) * 10_000 if slow else 0.0
        cost = self.costs.for_dex_leg(notional, sol_usd, q.price_impact_bps)
        net_bps = abs(spread_bps) - cost.total_bps
        pos = self.ledger.position(pair.name)
        held = pos.quantity
        last_ts = self._last_action.get(pair.name, 0)
        meta = {"price": price, "ema_fast": fast, "ema_slow": slow,
                "spread_bps": spread_bps, "costs": cost.describe()}

        if time.time() - last_ts < self.cfg.momentum_min_hold_seconds and held > 0:
            return hold(pair, "inside minimum hold window", meta=meta)
        if net_bps < self.cfg.min_edge_bps:
            return hold(pair, f"trend {spread_bps:+.1f}bps net {net_bps:+.1f}bps under floor", meta=meta)

        if spread_bps > 0 and held * price < self.cfg.max_position_usd:
            self._last_action[pair.name] = time.time()
            return Signal(action=BUY, pair=pair, reason=f"fast EMA {spread_bps:+.1f}bps above slow",
                          edge_bps=net_bps, gross_bps=spread_bps, cost_bps=cost.total_bps,
                          notional_usd=notional, in_token=quote_tok, out_token=base_tok,
                          in_amount=buy_in, quote=q, meta=meta)
        if spread_bps < 0 and held > 0:
            sell_q = await self.market.dex_quote(base_tok, quote_tok, held)
            self._last_action[pair.name] = time.time()
            return Signal(action=SELL, pair=pair, reason=f"fast EMA {spread_bps:+.1f}bps below slow",
                          edge_bps=net_bps, gross_bps=abs(spread_bps), cost_bps=cost.total_bps,
                          notional_usd=held * price, in_token=base_tok, out_token=quote_tok,
                          in_amount=held, quote=sell_q, meta=meta)
        return hold(pair, "trend present but nothing to do at current inventory", meta=meta)


STRATEGY_CLASSES = {
    "roundtrip": RoundTripStrategy,
    "basis": BasisStrategy,
    "momentum": MomentumStrategy,
}


def build_strategy(cfg, market, ledger):
    try:
        cls = STRATEGY_CLASSES[cfg.strategy]
    except KeyError:
        raise ValueError(f"unknown strategy {cfg.strategy!r}, pick one of {sorted(STRATEGY_CLASSES)}") from None
    return cls(cfg, market, ledger)
