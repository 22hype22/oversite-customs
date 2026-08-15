"""The trading loop.

    quote -> signal -> risk gate -> execute -> record -> notify

Lifecycle is two independent switches, and the distinction matters:

    start/stop   the loop runs and evaluates
    arm/disarm   orders are allowed to leave the process

A started-but-disarmed engine is the useful default: it prints exactly what it
would have traded, with the same cost model and the same risk vetoes, without
spending anything. Live mode always boots disarmed.
"""

import asyncio
import time

import httpx

from .config import LIVE, PAPER
from .execution import ExecutionError, build_executor
from .ledger import Ledger
from .markets import STABLES
from .notifier import LogNotifier, MultiNotifier
from .quotes import MarketData, QuoteError
from .risk import RiskManager
from .strategy import build_strategy

MAX_CONSECUTIVE_ERRORS = 12


class TradingEngine:
    def __init__(self, cfg, ledger=None, notifier=None, http=None, market=None):
        cfg.validate()
        self.cfg = cfg
        self.ledger = ledger or Ledger(cfg.db_path)
        self.notifier = notifier or LogNotifier()
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(
            headers={"User-Agent": "oversite-crypto/1.0"}, trust_env=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
        )
        self.market = market or MarketData(cfg, self.http)
        self.strategy = build_strategy(cfg, self.market, self.ledger)
        self.risk = RiskManager(cfg, self.ledger)
        self.executor = build_executor(cfg, self.market, self.http)

        self._task = None
        self._stopping = asyncio.Event()
        self.running = False
        self.started_at = 0.0
        self.ticks = 0
        self.errors = 0
        self.consecutive_errors = 0
        self.last_error = ""
        self.last_tick_at = 0.0
        self.last_signals = {}     # pair name -> last Signal
        self.last_sol_usd = 0.0
        self.preflight_notes = []

    # ------------------------------------------------------------- lifecycle
    async def preflight(self):
        """Checks that must pass before the engine is trusted with money.

        Returns a list of problems. Empty means clean. In live mode a non-empty
        list is fatal; in paper mode it is a warning.
        """
        problems = []
        if self.cfg.verify_mints:
            symbols = sorted({t.symbol for p in self.cfg.pairs for t in (p.base, p.quote)})
            ok, issues = await self.cfg.registry.verify_mints(
                self.http, symbols, self.cfg.jupiter_token_url)
            if not ok:
                problems.extend(issues)
        try:
            sol = await self.market.sol_usd()
            self.last_sol_usd = sol
            if sol <= 0:
                problems.append("SOL/USD price came back as zero")
        except Exception as e:
            problems.append(f"cannot price SOL/USD (gas cannot be costed): {e!r}")

        if self.cfg.strategy == "basis":
            for pair in self.cfg.pairs:
                ref = await self.market.reference(pair.base)
                if not ref:
                    problems.append(
                        f"basis strategy has no {self.cfg.reference_venue} reference for "
                        f"{pair.base.symbol} — set CRYPTO_REFERENCE_VENUE or add a cex symbol")

        if self.cfg.is_live:
            try:
                problems.extend(await self.executor.preflight_checks(self.last_sol_usd))
            except ExecutionError as e:
                problems.append(str(e))
            except Exception as e:
                problems.append(f"wallet preflight failed: {e!r}")

        self.preflight_notes = problems
        return problems

    async def start(self, arm=False):
        if self.running:
            return False, "already running"
        problems = await self.preflight()
        if problems and self.cfg.is_live:
            msg = "preflight failed:\n  - " + "\n  - ".join(problems)
            self.ledger.record_event("error", "preflight", msg)
            await self.notifier.notify("error", "Preflight failed", msg)
            return False, msg
        if problems:
            await self.notifier.notify(
                "warn", "Preflight warnings",
                "Running in paper mode anyway:\n  - " + "\n  - ".join(problems))

        self._stopping.clear()
        self.running = True
        self.started_at = time.time()
        self.consecutive_errors = 0
        if arm:
            self.risk.arm("start")
        self._task = asyncio.create_task(self._run(), name="crypto-engine")
        self.ledger.record_event("info", "start", f"engine started in {self.cfg.mode} mode",
                                 {"strategy": self.cfg.strategy,
                                  "pairs": [p.name for p in self.cfg.pairs]})
        await self.notifier.notify(
            "info", "Engine started",
            f"{self.cfg.mode.upper()} · {self.cfg.strategy} · {', '.join(p.name for p in self.cfg.pairs)}",
            {"armed": self.risk.armed})
        return True, "started"

    async def stop(self, reason="manual"):
        if not self.running:
            return False, "not running"
        self.running = False
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self.cfg.poll_seconds + 30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
        self._task = None
        self.risk.disarm("stop", reason)
        self.ledger.record_event("info", "stop", f"engine stopped ({reason})")
        await self.notifier.notify("info", "Engine stopped", reason)
        return True, "stopped"

    async def close(self):
        await self.stop("shutdown")
        if self._own_http:
            try:
                await self.http.aclose()
            except Exception:
                pass
        self.ledger.close()

    def arm(self, who="system"):
        if self.risk.halted:
            return False, f"halted: {self.risk.halt_reason} — clear it with /crypto resume"
        self.risk.arm(who)
        return True, f"armed ({self.cfg.mode})"

    def disarm(self, who="system", reason=""):
        self.risk.disarm(who, reason)
        return True, "disarmed"

    def panic(self, who="system"):
        """Kill switch: stop trading immediately, keep the loop observing."""
        self.risk.halt(f"panic by {who}")
        return True, "halted — no further orders until resumed"

    def resume(self, who="system"):
        self.risk.halted = False
        self.risk.halt_reason = ""
        self.ledger.record_event("info", "resume", f"halt cleared by {who}")
        return True, "halt cleared (still disarmed — arm to trade)"

    # ------------------------------------------------------------------ loop
    async def _run(self):
        while self.running and not self._stopping.is_set():
            began = time.time()
            try:
                await self.tick()
                self.consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors += 1
                self.consecutive_errors += 1
                self.last_error = repr(e)
                self.ledger.record_event("error", "tick", repr(e))
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self.risk.halt(f"{self.consecutive_errors} consecutive tick failures: {e!r}")
                    await self.notifier.notify(
                        "error", "Engine halted",
                        f"{self.consecutive_errors} consecutive failures. Last: {e!r}")
                elif self.consecutive_errors in (3, 6):
                    await self.notifier.notify("warn", "Tick failing", repr(e))
            # Back off on sustained failure instead of hammering the endpoint.
            delay = self.cfg.poll_seconds * (1 + min(self.consecutive_errors, 5))
            elapsed = time.time() - began
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(0.5, delay - elapsed))
            except asyncio.TimeoutError:
                pass

    async def tick(self):
        self.ticks += 1
        self.last_tick_at = time.time()
        # A failure here is fatal to the tick on purpose: without a SOL price
        # there is no way to cost gas, and an uncosted trade is a blind trade.
        self.last_sol_usd = await self.market.sol_usd()
        sol_usd = self.last_sol_usd

        for pair in self.cfg.pairs:
            try:
                signal = await self.strategy.evaluate(pair, sol_usd)
            except QuoteError as e:
                self.ledger.record_event("warn", "quote", f"{pair}: {e}")
                continue
            self.last_signals[pair.name] = signal

            if not signal.actionable:
                continue

            decision = self.risk.check(signal, sol_usd)
            if not decision:
                self.ledger.record_event("info", "veto", f"{pair}: {decision.reason}",
                                         {"edge_bps": signal.edge_bps, "action": signal.action})
                if not self.risk.armed and signal.edge_bps >= self.cfg.min_edge_bps:
                    await self.notifier.notify(
                        "info", "Signal (not traded)", signal.summary(),
                        {"why": decision.reason})
                continue

            await self._execute(signal, sol_usd)

        if self.ticks % 25 == 0:
            self.ledger.snapshot_equity(self.equity_usd())

    async def _execute(self, signal, sol_usd):
        self.risk.note_trade_started()
        try:
            result = await self.executor.execute(signal, sol_usd)
        except ExecutionError as e:
            self.risk.note_trade_finished()
            self.ledger.record_event("error", "execute", f"{signal.pair}: {e}")
            await self.notifier.notify("error", "Execution error", f"{signal.pair}: {e}")
            return
        except Exception as e:
            self.risk.note_trade_finished()
            self.ledger.record_event("error", "execute", f"{signal.pair}: {e!r}")
            await self.notifier.notify("error", "Execution error", f"{signal.pair}: {e!r}")
            return
        self.risk.note_trade_finished()

        if result.status == "rejected":
            self.ledger.record_event("warn", "rejected", f"{signal.pair}: {result.error}")
            await self.notifier.notify("warn", "Order rejected", f"{signal.pair}: {result.error}")
            return

        price_usd, notional_usd = self._price_and_notional(signal, result, sol_usd)
        realized = self.ledger.record_fill(
            mode=self.cfg.mode, strategy=self.cfg.strategy, pair=signal.pair.name,
            action=signal.action, in_symbol=signal.in_token.symbol, in_amount=result.in_amount,
            out_symbol=signal.out_token.symbol, out_amount=result.out_amount,
            price_usd=price_usd, notional_usd=notional_usd, fee_usd=result.fee_usd,
            edge_bps=signal.edge_bps, tx_sig=result.tx_sig,
            status=result.status, note=signal.reason[:200],
        )

        if result.status != "filled":
            await self.notifier.notify(
                "error", "Transaction failed",
                f"{signal.pair} {signal.action} — {result.error}",
                {"fee_burned": f"${result.fee_usd:.4f}", "sig": result.tx_sig or "n/a"})
            return

        fields = {
            "edge": f"{signal.edge_bps:+.1f}bps",
            "in": f"{result.in_amount:,.6f} {signal.in_token.symbol}",
            "out": f"{result.out_amount:,.6f} {signal.out_token.symbol}",
            "fee": f"${result.fee_usd:.4f}",
        }
        if signal.action == "SELL":
            fields["realized"] = f"${realized:+,.4f}"
        if result.tx_sig:
            fields["tx"] = f"https://solscan.io/tx/{result.tx_sig}"
        await self.notifier.notify(
            "trade", f"{self.cfg.mode.upper()} {signal.action} {signal.pair}",
            signal.reason, fields)

        day_pnl = self.ledger.today_pnl()
        if day_pnl <= -abs(self.cfg.max_daily_loss_usd):
            self.risk.halt(f"daily loss limit hit: ${day_pnl:,.2f}")
            await self.notifier.notify("error", "Daily loss limit",
                                       f"Down ${abs(day_pnl):,.2f} today — trading halted.")

    def _price_and_notional(self, signal, result, sol_usd):
        """USD price of the base asset and USD notional of this fill."""
        quote = signal.pair.quote
        quote_usd = 1.0 if quote.symbol.upper() in STABLES else (
            sol_usd if quote.symbol.upper() == "SOL" else 0.0)

        if signal.action == "BUY":
            base_qty = result.out_amount
            quote_spent = result.in_amount
        else:
            base_qty = result.in_amount
            quote_spent = result.out_amount

        notional = quote_spent * quote_usd
        price = (notional / base_qty) if base_qty else 0.0
        if not quote_usd:
            # Unpriceable quote asset — fall back to the strategy's own view.
            notional = signal.notional_usd
            price = (notional / base_qty) if base_qty else 0.0
        return price, notional

    # ---------------------------------------------------------------- status
    def equity_usd(self):
        total = 0.0
        for pos in self.ledger.positions():
            mark = 0.0
            sig = self.last_signals.get(pos.pair)
            if sig:
                mark = sig.meta.get("ref_mid") or sig.meta.get("price") or 0.0
            total += pos.quantity * mark if mark else pos.cost_basis
        return total

    def status(self):
        stats = self.ledger.stats()
        today = self.ledger.stats(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%d"))
        return {
            "running": self.running,
            "mode": self.cfg.mode,
            "strategy": self.cfg.strategy,
            "pairs": [p.name for p in self.cfg.pairs],
            "uptime_s": (time.time() - self.started_at) if self.started_at else 0,
            "ticks": self.ticks,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_tick_age_s": (time.time() - self.last_tick_at) if self.last_tick_at else None,
            "sol_usd": self.last_sol_usd,
            "risk": self.risk.status(),
            "all_time": stats,
            "today": today,
            "open_position_value_usd": self.equity_usd(),
            "preflight_notes": self.preflight_notes,
            "signals": {k: v.summary() for k, v in self.last_signals.items()},
        }


async def build_engine(cfg, notifier=None):
    return TradingEngine(cfg, notifier=notifier)


__all__ = ["TradingEngine", "build_engine", "LIVE", "PAPER", "MultiNotifier"]
