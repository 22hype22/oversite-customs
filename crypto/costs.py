"""The cost model.

This is the part that decides whether a "profitable" spread is actually
profitable. Every cost below is real and routinely larger than the spread it is
being compared against:

  * network fees — base fee + priority fee, per transaction, paid whether the
    swap lands or reverts;
  * price impact — already inside the aggregator quote, but charged again on
    the exit leg of a round trip;
  * slippage tolerance — the difference between the quote and the worst fill
    you agreed to accept;
  * reference-venue taker fee — only for strategies that model a CEX leg.

A strategy that skips any of these will report edge that does not exist.
"""

from dataclasses import dataclass

LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class CostBreakdown:
    gas_usd: float = 0.0
    impact_bps: float = 0.0
    slippage_bps: float = 0.0
    venue_fee_bps: float = 0.0
    notional_usd: float = 0.0

    @property
    def gas_bps(self) -> float:
        if self.notional_usd <= 0:
            return 0.0
        return (self.gas_usd / self.notional_usd) * 10_000

    @property
    def total_bps(self) -> float:
        return self.gas_bps + self.impact_bps + self.slippage_bps + self.venue_fee_bps

    @property
    def total_usd(self) -> float:
        return self.gas_usd + (self.notional_usd * (self.total_bps - self.gas_bps) / 10_000)

    def describe(self) -> str:
        return (
            f"gas ${self.gas_usd:.4f} ({self.gas_bps:.1f}bps) + impact {self.impact_bps:.1f}bps "
            f"+ slip {self.slippage_bps:.1f}bps + venue {self.venue_fee_bps:.1f}bps "
            f"= {self.total_bps:.1f}bps"
        )


class CostModel:
    def __init__(self, cfg):
        self.cfg = cfg

    def gas_usd(self, sol_usd: float, legs: int = 1) -> float:
        """Cost of `legs` on-chain transactions, in USD.

        Priority fee is configured in lamports and treated as a flat per-tx
        cost. Failed transactions still burn the base fee — the engine counts
        those separately via record_failed_tx.
        """
        lamports = (self.cfg.base_fee_lamports + self.cfg.priority_fee_lamports) * max(1, int(legs))
        return (lamports / LAMPORTS_PER_SOL) * max(0.0, sol_usd)

    def failed_tx_usd(self, sol_usd: float) -> float:
        return (self.cfg.base_fee_lamports / LAMPORTS_PER_SOL) * max(0.0, sol_usd)

    def for_dex_leg(self, notional_usd, sol_usd, impact_bps, include_slippage=True):
        return CostBreakdown(
            gas_usd=self.gas_usd(sol_usd, legs=1),
            impact_bps=max(0.0, float(impact_bps)),
            slippage_bps=(self.cfg.max_slippage_bps if include_slippage else 0.0),
            venue_fee_bps=0.0,
            notional_usd=max(0.0, float(notional_usd)),
        )

    def for_round_trip(self, notional_usd, sol_usd, impact_bps_total):
        """Both legs of an A->B->A trip: two transactions, impact on each."""
        return CostBreakdown(
            gas_usd=self.gas_usd(sol_usd, legs=2),
            impact_bps=max(0.0, float(impact_bps_total)),
            slippage_bps=self.cfg.max_slippage_bps * 2,
            venue_fee_bps=0.0,
            notional_usd=max(0.0, float(notional_usd)),
        )

    def for_basis(self, notional_usd, sol_usd, impact_bps):
        """DEX leg now, CEX leg later — model both fees, one on-chain tx."""
        return CostBreakdown(
            gas_usd=self.gas_usd(sol_usd, legs=1),
            impact_bps=max(0.0, float(impact_bps)),
            slippage_bps=self.cfg.max_slippage_bps,
            venue_fee_bps=self.cfg.cex_taker_fee_bps,
            notional_usd=max(0.0, float(notional_usd)),
        )
