"""Headless runner: ``python -m crypto.cli <command>``.

    selftest    config + connectivity + mint verification, trades nothing
    quote       one-off executable quote for a pair
    signal      evaluate the strategy once and print the decision
    run         the loop (paper unless CRYPTO_MODE=live; --arm to allow orders)
    status      engine-less summary read straight from the ledger
    pnl         realized PnL, fees, win rate
    positions   current inventory
"""

import argparse
import asyncio
import json
import signal as _signal
import sys

import httpx

from .config import load_config
from .engine import TradingEngine
from .ledger import Ledger
from .markets import parse_pairs
from .notifier import LogNotifier


def _client():
    return httpx.AsyncClient(headers={"User-Agent": "oversite-crypto/1.0"},
                             trust_env=True, timeout=httpx.Timeout(20.0, connect=10.0))


async def cmd_selftest(args):
    cfg = load_config()
    print("Config")
    print(json.dumps(cfg.redacted(), indent=2, default=str))
    try:
        cfg.validate()
        print("\n[ok] config validates")
    except ValueError as e:
        print(f"\n[FAIL] {e}")
        return 1
    engine = TradingEngine(cfg, notifier=LogNotifier())
    try:
        problems = await engine.preflight()
        if problems:
            print("\nPreflight problems:")
            for p in problems:
                print(f"  - {p}")
            print("\n[FAIL] not safe to trade live" if cfg.is_live else "\n[warn] paper mode can still run")
            return 1 if cfg.is_live else 0
        print("[ok] preflight clean")
        print(f"[ok] SOL/USD = ${engine.last_sol_usd:,.2f}")
        return 0
    finally:
        await engine.close()


async def cmd_quote(args):
    cfg = load_config()
    pairs = parse_pairs(args.pair, cfg.registry)
    async with _client() as http:
        from .quotes import MarketData
        md = MarketData(cfg, http)
        sol = await md.sol_usd()
        print(f"SOL/USD ${sol:,.2f}")
        for pair in pairs:
            amount = args.amount if args.amount else cfg.base_order_usd
            q = await md.dex_quote(pair.quote, pair.base, amount)
            got = pair.base.from_atoms(q.out_atoms)
            print(f"\n{pair}: {amount:g} {pair.quote.symbol} -> {got:,.6f} {pair.base.symbol}")
            print(f"  price      {amount / got:,.6f} {pair.quote.symbol}/{pair.base.symbol}")
            print(f"  impact     {q.price_impact_bps:.2f} bps")
            print(f"  worst fill {pair.base.from_atoms(q.min_out_atoms):,.6f} {pair.base.symbol}")
            ref = await md.reference(pair.base)
            if ref:
                print(f"  {ref.venue:9s} bid {ref.bid:,.6f} / ask {ref.ask:,.6f} "
                      f"(spread {ref.spread_bps:.1f}bps)")
    return 0


async def cmd_signal(args):
    cfg = load_config()
    cfg.validate()
    engine = TradingEngine(cfg, notifier=LogNotifier())
    try:
        sol = await engine.market.sol_usd()
        print(f"SOL/USD ${sol:,.2f}  strategy={cfg.strategy}\n")
        for pair in cfg.pairs:
            sig = await engine.strategy.evaluate(pair, sol)
            print(sig.summary())
            if sig.meta:
                print("   " + json.dumps(sig.meta, default=lambda o: round(o, 6)
                                         if isinstance(o, float) else str(o)))
            if sig.actionable:
                d = engine.risk.check(sig, sol)
                print(f"   risk: {'ALLOW' if d.ok else 'VETO — ' + d.reason}")
    finally:
        await engine.close()
    return 0


async def cmd_run(args):
    cfg = load_config()
    if args.paper:
        cfg = cfg.with_overrides(mode="paper")
    cfg.enabled = True
    cfg.validate()
    engine = TradingEngine(cfg, notifier=LogNotifier())

    stop = asyncio.Event()

    def _bye(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _bye)
        except NotImplementedError:
            pass

    ok, msg = await engine.start(arm=False)
    if not ok:
        print(f"[FAIL] {msg}")
        await engine.close()
        return 1
    if args.arm:
        armed, m = engine.arm("cli")
        print(f"[arm] {m}" if armed else f"[arm failed] {m}")
    else:
        print("[note] running DISARMED — signals only. Re-run with --arm to place orders.")
    try:
        await stop.wait()
    finally:
        print("\nshutting down…")
        await engine.close()
    return 0


async def cmd_status(args):
    cfg = load_config()
    led = Ledger(cfg.db_path)
    print(json.dumps({
        "db": cfg.db_path,
        "all_time": led.stats(),
        "positions": [vars(p) for p in led.positions()],
        "recent": led.recent_fills(5),
    }, indent=2, default=str))
    led.close()
    return 0


async def cmd_pnl(args):
    cfg = load_config()
    led = Ledger(cfg.db_path)
    s = led.stats(args.day)
    scope = args.day or "all time"
    print(f"PnL ({scope})")
    print(f"  trades      {s['trades']}  (failed txs {s['failed_txs']})")
    print(f"  win rate    {s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L)")
    print(f"  fees paid   ${s['fees_usd']:,.4f}")
    print(f"  net PnL     ${s['pnl_usd']:+,.4f}   <- already net of fees")
    led.close()
    return 0


async def cmd_positions(args):
    cfg = load_config()
    led = Ledger(cfg.db_path)
    rows = led.positions()
    if not rows:
        print("no positions")
    for p in rows:
        print(f"{p.pair:12s} qty {p.quantity:,.6f} @ avg ${p.avg_cost:,.6f} "
              f"| realized ${p.realized:+,.4f} | fees ${p.fees:,.4f}")
    led.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crypto", description="Oversite crypto trading engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="validate config and connectivity")

    q = sub.add_parser("quote", help="one-off executable quote")
    q.add_argument("pair", nargs="?", default="SOL/USDC")
    q.add_argument("--amount", type=float, default=0.0, help="quote-token amount (default: base order size)")

    sub.add_parser("signal", help="evaluate the strategy once")

    r = sub.add_parser("run", help="run the trading loop")
    r.add_argument("--arm", action="store_true", help="allow orders (otherwise signals only)")
    r.add_argument("--paper", action="store_true", help="force paper mode regardless of CRYPTO_MODE")

    sub.add_parser("status", help="ledger summary")
    p = sub.add_parser("pnl", help="realized PnL")
    p.add_argument("--day", help="YYYY-MM-DD (UTC)")
    sub.add_parser("positions", help="current inventory")

    args = ap.parse_args(argv)
    fn = {
        "selftest": cmd_selftest, "quote": cmd_quote, "signal": cmd_signal,
        "run": cmd_run, "status": cmd_status, "pnl": cmd_pnl, "positions": cmd_positions,
    }[args.cmd]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
