"""Risk gate.

Every actionable signal passes through ``RiskManager.check`` before any
executor sees it. The manager is deliberately boring and refuses by default:
if a check cannot be evaluated (missing price, stale quote, unknown position)
the answer is no.

The arm/disarm flag is the kill switch. It is separate from start/stop on
purpose — a running engine that is disarmed keeps evaluating and logging
signals so you can watch what it *would* have done without it trading.
"""

import time
from dataclasses import dataclass


@dataclass
class Decision:
    ok: bool
    reason: str = ""

    def __bool__(self):
        return self.ok


ALLOW = Decision(True)


class RiskManager:
    def __init__(self, cfg, ledger):
        self.cfg = cfg
        self.ledger = ledger
        self.armed = False
        self.halted = False
        self.halt_reason = ""
        self.last_trade_ts = 0.0
        self.open_trades = 0
        self.session_start = time.time()

    # -------------------------------------------------------------- control
    def arm(self, who="system"):
        """Allow orders. A halt is deliberately NOT cleared here — clearing it
        is a separate, explicit act (resume), so a kill switch can never be
        undone by muscle memory."""
        if self.halted:
            return False
        self.armed = True
        self.ledger.record_event("info", "arm", f"armed by {who}")
        return True

    def disarm(self, who="system", reason=""):
        self.armed = False
        self.ledger.record_event("info", "disarm", f"disarmed by {who}{': ' + reason if reason else ''}")

    def halt(self, reason):
        """Hard stop — survives until someone explicitly re-arms."""
        self.halted = True
        self.armed = False
        self.halt_reason = reason
        self.ledger.record_event("error", "halt", reason)

    # ---------------------------------------------------------------- gates
    def check(self, signal, sol_usd) -> Decision:
        cfg = self.cfg

        if self.halted:
            return Decision(False, f"halted: {self.halt_reason}")
        if not self.armed:
            return Decision(False, "engine is disarmed (arm it to allow orders)")
        if not signal.actionable:
            return Decision(False, "no actionable signal")

        if signal.quote is None:
            return Decision(False, "signal carries no executable quote")
        age = getattr(signal.quote, "age", 0)
        if age > cfg.quote_stale_seconds:
            return Decision(False, f"quote is {age:.1f}s stale (limit {cfg.quote_stale_seconds:.0f}s)")

        if signal.edge_bps < cfg.min_edge_bps:
            return Decision(False, f"edge {signal.edge_bps:.1f}bps under floor {cfg.min_edge_bps:.0f}bps")

        impact = getattr(signal.quote, "price_impact_bps", 0.0)
        if impact > cfg.max_price_impact_bps:
            return Decision(False, f"price impact {impact:.0f}bps over limit {cfg.max_price_impact_bps:.0f}bps")

        if signal.notional_usd <= 0:
            return Decision(False, "notional is zero")
        if signal.notional_usd > cfg.max_position_usd:
            return Decision(False, f"order ${signal.notional_usd:,.2f} exceeds position cap "
                                   f"${cfg.max_position_usd:,.2f}")

        since = time.time() - self.last_trade_ts
        if since < cfg.trade_cooldown_seconds:
            return Decision(False, f"cooling down ({cfg.trade_cooldown_seconds - since:.0f}s left)")

        if self.open_trades >= cfg.max_open_trades:
            return Decision(False, f"{self.open_trades} trades already in flight (max {cfg.max_open_trades})")

        day_pnl = self.ledger.today_pnl()
        if day_pnl <= -abs(cfg.max_daily_loss_usd):
            self.halt(f"daily loss limit hit: ${day_pnl:,.2f}")
            return Decision(False, self.halt_reason)
        # Would this trade's worst case breach the limit? Treat the full
        # modeled cost as the immediate downside.
        worst = signal.notional_usd * (signal.cost_bps / 10_000)
        if day_pnl - worst <= -abs(cfg.max_daily_loss_usd):
            return Decision(False, f"trade could breach the daily loss limit (${day_pnl:,.2f} today)")

        losses = self.ledger.consecutive_losses()
        if losses >= cfg.max_consecutive_losses:
            self.halt(f"{losses} losing trades in a row")
            return Decision(False, self.halt_reason)

        if signal.action == "SELL":
            pos = self.ledger.position(signal.pair.name)
            if signal.in_amount > pos.quantity + 1e-9:
                return Decision(False, f"cannot sell {signal.in_amount:.6f} — holding {pos.quantity:.6f}")
        else:
            pos = self.ledger.position(signal.pair.name)
            mark = signal.meta.get("ref_mid") or signal.meta.get("price") or 0.0
            held_usd = pos.quantity * mark if mark else pos.cost_basis
            if held_usd + signal.notional_usd > cfg.max_position_usd + 1e-9:
                return Decision(False, f"position cap: ${held_usd:,.2f} held + ${signal.notional_usd:,.2f} "
                                       f"> ${cfg.max_position_usd:,.2f}")

        if cfg.is_live and sol_usd <= 0:
            return Decision(False, "no SOL/USD price — cannot price gas, refusing to trade blind")

        return ALLOW

    # ------------------------------------------------------------ accounting
    def note_trade_started(self):
        self.open_trades += 1
        self.last_trade_ts = time.time()

    def note_trade_finished(self):
        self.open_trades = max(0, self.open_trades - 1)

    def status(self):
        return {
            "armed": self.armed,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "open_trades": self.open_trades,
            "today_pnl": self.ledger.today_pnl(),
            "daily_loss_limit": self.cfg.max_daily_loss_usd,
            "consecutive_losses": self.ledger.consecutive_losses(),
            "seconds_since_trade": (time.time() - self.last_trade_ts) if self.last_trade_ts else None,
        }
