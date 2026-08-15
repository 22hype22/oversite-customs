"""Tests for the crypto trading engine.

Everything runs against httpx.MockTransport — no network, no wallet, no money.
The point of these is the arithmetic and the refusals: a cost model that
under-reports and a risk gate that lets one bad trade through are the two ways
this thing loses real funds.
"""

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto.config import TradingConfig, load_config
from crypto.costs import CostModel
from crypto.engine import TradingEngine
from crypto.execution import PaperExecutor
from crypto.ledger import Ledger
from crypto.markets import Registry, parse_pairs
from crypto.quotes import MarketData, QuoteError
from crypto.risk import RiskManager
from crypto.strategy import BasisStrategy, RoundTripStrategy, build_strategy

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# --------------------------------------------------------------- fixtures
class FakeChain:
    """A toy market: SOL trades at `dex_price` on the DEX, `ref_price` on the CEX."""

    def __init__(self, dex_price=100.0, ref_price=100.0, impact_bps=5.0, spread_bps=4.0):
        self.dex_price = dex_price
        self.ref_price = ref_price
        self.impact_bps = impact_bps
        self.spread_bps = spread_bps
        self.quote_calls = 0
        self.swap_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/quote" in url:
            self.quote_calls += 1
            p = request.url.params
            amount = int(p["amount"])
            in_mint, out_mint = p["inputMint"], p["outputMint"]
            if in_mint == USDC_MINT and out_mint == SOL_MINT:
                usdc = amount / 1e6
                sol = usdc / self.dex_price
                out = int(sol * 1e9)
            elif in_mint == SOL_MINT and out_mint == USDC_MINT:
                sol = amount / 1e9
                usdc = sol * self.dex_price
                out = int(usdc * 1e6)
            else:
                return httpx.Response(400, json={"error": "unknown pair"})
            out = int(out * (1 - self.impact_bps / 10_000))
            return httpx.Response(200, json={
                "inputMint": in_mint, "outputMint": out_mint,
                "inAmount": str(amount), "outAmount": str(out),
                "otherAmountThreshold": str(int(out * 0.995)),
                "priceImpactPct": str(self.impact_bps / 10_000),
                "slippageBps": p.get("slippageBps", "50"),
                "routePlan": [{"swapInfo": {"label": "FakeAMM"}}],
            })
        if "/swap" in url:
            self.swap_calls += 1
            return httpx.Response(200, json={"swapTransaction": "AA=="})
        if "bookTicker" in url:
            half = self.ref_price * (self.spread_bps / 2) / 10_000
            return httpx.Response(200, json={
                "symbol": "SOLUSDT",
                "bidPrice": f"{self.ref_price - half}",
                "askPrice": f"{self.ref_price + half}",
            })
        if "/tokens/" in url or "search" in url:
            return httpx.Response(200, json=[
                {"id": SOL_MINT, "decimals": 9, "symbol": "SOL"},
                {"id": USDC_MINT, "decimals": 6, "symbol": "USDC"},
            ])
        return httpx.Response(404, json={"error": f"unrouted {url}"})


def make_cfg(tmp_path, **over):
    reg = Registry()
    cfg = TradingConfig(
        enabled=True, mode="paper", strategy="basis",
        registry=reg, pairs=parse_pairs("SOL/USDC", reg),
        base_order_usd=100.0, max_position_usd=1000.0, max_daily_loss_usd=50.0,
        min_edge_bps=20.0, max_slippage_bps=50.0, max_price_impact_bps=100.0,
        priority_fee_lamports=200_000, base_fee_lamports=5_000,
        cex_taker_fee_bps=10.0, poll_seconds=1.0, trade_cooldown_seconds=0.0,
        db_path=str(tmp_path / "t.db"), verify_mints=False,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def http(chain):
    return httpx.AsyncClient(transport=httpx.MockTransport(chain.handler))


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(str(tmp_path / "ledger.db"))
    yield led
    led.close()


# ------------------------------------------------------------------ markets
def test_atoms_round_trip():
    reg = Registry()
    sol = reg.get("SOL")
    assert sol.to_atoms(1.5) == 1_500_000_000
    assert sol.from_atoms(1_500_000_000) == 1.5
    usdc = reg.get("usdc")            # case-insensitive
    assert usdc.to_atoms(2.5) == 2_500_000


def test_parse_pairs_rejects_garbage():
    reg = Registry()
    with pytest.raises(ValueError):
        parse_pairs("SOLUSDC", reg)
    with pytest.raises(ValueError):
        parse_pairs("SOL/SOL", reg)
    with pytest.raises(ValueError):
        parse_pairs("", reg)
    with pytest.raises(KeyError):
        parse_pairs("NOTATOKEN/USDC", reg)


def test_registry_env_override():
    reg = Registry.from_env(json.dumps({"FOO": {"mint": "abc", "decimals": 4, "cex": "FOOUSDT"}}))
    assert reg.get("FOO").decimals == 4
    assert reg.get("FOO").cex_symbol == "FOOUSDT"


# -------------------------------------------------------------------- config
def test_config_validation_catches_unsafe_combinations(tmp_path):
    cfg = make_cfg(tmp_path, mode="live", private_key="")
    with pytest.raises(ValueError, match="SOLANA_PRIVATE_KEY"):
        cfg.validate()

    cfg = make_cfg(tmp_path, max_position_usd=10.0, base_order_usd=100.0)
    with pytest.raises(ValueError, match="MAX_POSITION_USD"):
        cfg.validate()

    cfg = make_cfg(tmp_path, strategy="moonshot")
    with pytest.raises(ValueError, match="CRYPTO_STRATEGY"):
        cfg.validate()

    cfg = make_cfg(tmp_path, ema_fast=50, ema_slow=10)
    with pytest.raises(ValueError, match="EMA_FAST"):
        cfg.validate()


def test_config_never_leaks_the_key(tmp_path):
    cfg = make_cfg(tmp_path, private_key="supersecretkeymaterial")
    blob = json.dumps(cfg.redacted())
    assert "supersecretkeymaterial" not in blob
    assert cfg.redacted()["private_key"] == "set (hidden)"


def test_load_config_defaults_to_paper_and_disabled():
    cfg = load_config(env={"PATH": os.getenv("PATH", "")})
    assert cfg.mode == "paper"
    assert cfg.enabled is False


# --------------------------------------------------------------------- costs
def test_gas_is_priced_in_dollars(cfg):
    cm = CostModel(cfg)
    # (5_000 + 200_000) lamports = 0.000205 SOL; at $100 that's $0.0205
    assert cm.gas_usd(100.0, legs=1) == pytest.approx(0.0205)
    assert cm.gas_usd(100.0, legs=2) == pytest.approx(0.041)


def test_cost_breakdown_totals(cfg):
    cm = CostModel(cfg)
    cb = cm.for_basis(notional_usd=100.0, sol_usd=100.0, impact_bps=7.0)
    # gas 0.0205/100 = 2.05bps, impact 7, slippage 50, venue 10
    assert cb.gas_bps == pytest.approx(2.05)
    assert cb.total_bps == pytest.approx(2.05 + 7 + 50 + 10)


def test_small_size_makes_gas_dominate(cfg):
    """The whole reason $0.90 cannot compound: fees swamp tiny notionals."""
    cm = CostModel(cfg)
    tiny = cm.for_basis(notional_usd=0.90, sol_usd=200.0, impact_bps=5.0)
    assert tiny.gas_bps > 400          # >4% of the trade, before anything else
    big = cm.for_basis(notional_usd=5000.0, sol_usd=200.0, impact_bps=5.0)
    assert big.gas_bps < 1


# --------------------------------------------------------------------- ledger
def test_buy_then_sell_realizes_pnl(ledger):
    ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                       in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=1.0,
                       price_usd=100.0, notional_usd=100.0, fee_usd=0.02)
    pos = ledger.position("SOL/USDC")
    assert pos.quantity == pytest.approx(1.0)
    assert pos.avg_cost == pytest.approx(100.02)      # fee folded into basis

    realized = ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="SELL",
                                  in_symbol="SOL", in_amount=1.0, out_symbol="USDC", out_amount=110.0,
                                  price_usd=110.0, notional_usd=110.0, fee_usd=0.02)
    assert realized == pytest.approx(110.0 - 100.02 - 0.02)
    assert ledger.position("SOL/USDC").quantity == 0
    assert ledger.net_pnl() == pytest.approx(realized)


def test_failed_transaction_still_costs_money(ledger):
    ledger.record_fill(mode="live", strategy="basis", pair="SOL/USDC", action="BUY",
                       in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=0.0,
                       price_usd=0.0, notional_usd=0.0, fee_usd=0.001, status="failed")
    assert ledger.position("SOL/USDC").quantity == 0
    assert ledger.net_pnl() == pytest.approx(-0.001)
    assert ledger.stats()["failed_txs"] == 1


def test_average_cost_across_two_buys(ledger):
    for px in (100.0, 200.0):
        ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                           in_symbol="USDC", in_amount=px, out_symbol="SOL", out_amount=1.0,
                           price_usd=px, notional_usd=px, fee_usd=0.0)
    assert ledger.position("SOL/USDC").avg_cost == pytest.approx(150.0)


def test_consecutive_losses_counts_from_the_end(ledger):
    for pnl_price in (90.0, 80.0):    # two losing sells after a buy at 100
        ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                           in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=1.0,
                           price_usd=100.0, notional_usd=100.0, fee_usd=0.0)
        ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="SELL",
                           in_symbol="SOL", in_amount=1.0, out_symbol="USDC", out_amount=pnl_price,
                           price_usd=pnl_price, notional_usd=pnl_price, fee_usd=0.0)
    assert ledger.consecutive_losses() == 2


# ----------------------------------------------------------------------- risk
class _Sig:
    """Minimal stand-in for a Signal."""
    def __init__(self, **kw):
        self.action = kw.get("action", "BUY")
        self.pair = kw.get("pair")
        self.edge_bps = kw.get("edge_bps", 100.0)
        self.cost_bps = kw.get("cost_bps", 10.0)
        self.notional_usd = kw.get("notional_usd", 100.0)
        self.in_amount = kw.get("in_amount", 100.0)
        self.quote = kw.get("quote", _Q())
        self.meta = kw.get("meta", {})
        self.actionable = self.action in ("BUY", "SELL")


class _Q:
    def __init__(self, age=0.0, impact=5.0):
        self.age = age
        self.price_impact_bps = impact


def test_disarmed_engine_refuses_everything(cfg, ledger):
    rm = RiskManager(cfg, ledger)
    d = rm.check(_Sig(pair=cfg.pairs[0]), 100.0)
    assert not d and "disarmed" in d.reason


def test_risk_vetoes(cfg, ledger):
    rm = RiskManager(cfg, ledger)
    rm.arm("test")
    pair = cfg.pairs[0]

    assert not rm.check(_Sig(pair=pair, edge_bps=1.0), 100.0)             # under floor
    assert not rm.check(_Sig(pair=pair, quote=_Q(age=999)), 100.0)        # stale
    assert not rm.check(_Sig(pair=pair, quote=_Q(impact=9999)), 100.0)    # impact
    assert not rm.check(_Sig(pair=pair, notional_usd=10_000.0), 100.0)    # over cap
    assert not rm.check(_Sig(pair=pair, action="SELL", in_amount=5.0), 100.0)  # nothing held
    assert rm.check(_Sig(pair=pair), 100.0)                               # the good one


def test_cooldown_blocks_rapid_fire(tmp_path, ledger):
    cfg = make_cfg(tmp_path, trade_cooldown_seconds=60.0)
    rm = RiskManager(cfg, ledger)
    rm.arm("test")
    assert rm.check(_Sig(pair=cfg.pairs[0]), 100.0)
    rm.note_trade_started()
    d = rm.check(_Sig(pair=cfg.pairs[0]), 100.0)
    assert not d and "cooling down" in d.reason


def test_daily_loss_limit_halts(cfg, ledger):
    rm = RiskManager(cfg, ledger)
    rm.arm("test")
    ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                       in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=1.0,
                       price_usd=100.0, notional_usd=100.0, fee_usd=0.0)
    ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="SELL",
                       in_symbol="SOL", in_amount=1.0, out_symbol="USDC", out_amount=40.0,
                       price_usd=40.0, notional_usd=40.0, fee_usd=0.0)   # -$60, limit is $50
    d = rm.check(_Sig(pair=cfg.pairs[0]), 100.0)
    assert not d
    assert rm.halted and not rm.armed


def test_halt_cannot_be_undone_by_arming(cfg, ledger):
    rm = RiskManager(cfg, ledger)
    rm.arm("test")
    rm.halt("panic")
    assert rm.arm("test") is False       # arming must not clear a kill switch
    assert not rm.armed
    d = rm.check(_Sig(pair=cfg.pairs[0]), 100.0)
    assert not d and "halted" in d.reason


# --------------------------------------------------------------------- quotes
@pytest.mark.asyncio
async def test_dex_quote_parses(cfg, http, chain):
    md = MarketData(cfg, http)
    sol, usdc = cfg.registry.get("SOL"), cfg.registry.get("USDC")
    q = await md.dex_quote(usdc, sol, 100.0)
    assert q.in_atoms == 100_000_000
    assert sol.from_atoms(q.out_atoms) == pytest.approx(1.0 * (1 - 5 / 10_000), rel=1e-6)
    assert q.price_impact_bps == pytest.approx(5.0)
    assert q.min_out_atoms < q.out_atoms
    await http.aclose()


@pytest.mark.asyncio
async def test_reference_price(cfg, http):
    md = MarketData(cfg, http)
    ref = await md.reference(cfg.registry.get("SOL"))
    assert ref.mid == pytest.approx(100.0)
    assert ref.spread_bps == pytest.approx(4.0, abs=0.01)
    await http.aclose()


@pytest.mark.asyncio
async def test_bad_request_is_not_retried_forever(cfg):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad mint"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
        md = MarketData(cfg, h)
        with pytest.raises(QuoteError):
            await md.dex_quote(cfg.registry.get("USDC"), cfg.registry.get("SOL"), 10.0)
    assert calls["n"] == 1


# ------------------------------------------------------------------ strategy
@pytest.mark.asyncio
async def test_basis_holds_when_venues_agree(cfg, http, ledger):
    md = MarketData(cfg, http)
    strat = BasisStrategy(cfg, md, ledger)
    sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable
    assert "floor" in sig.reason
    await http.aclose()


@pytest.mark.asyncio
async def test_basis_buys_when_dex_is_cheap(cfg, tmp_path, ledger):
    chain = FakeChain(dex_price=95.0, ref_price=100.0, impact_bps=2.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        md = MarketData(cfg, h)
        strat = BasisStrategy(cfg, md, ledger)
        sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert sig.action == "BUY"
    assert sig.edge_bps > cfg.min_edge_bps
    assert sig.edge_bps < sig.gross_bps          # costs were actually subtracted


@pytest.mark.asyncio
async def test_basis_will_not_sell_what_it_does_not_hold(cfg, ledger):
    chain = FakeChain(dex_price=105.0, ref_price=100.0, impact_bps=2.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        strat = BasisStrategy(cfg, MarketData(cfg, h), ledger)
        sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable
    assert "no SOL held" in sig.reason


@pytest.mark.asyncio
async def test_basis_sells_inventory_into_a_rich_dex(cfg, ledger):
    ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                       in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=2.0,
                       price_usd=50.0, notional_usd=100.0, fee_usd=0.0)
    chain = FakeChain(dex_price=105.0, ref_price=100.0, impact_bps=2.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        strat = BasisStrategy(cfg, MarketData(cfg, h), ledger)
        sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert sig.action == "SELL"


@pytest.mark.asyncio
async def test_basis_rejects_absurd_divergence_as_bad_data(cfg, ledger):
    chain = FakeChain(dex_price=1.0, ref_price=100.0)     # 99% off — not an opportunity
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        strat = BasisStrategy(cfg, MarketData(cfg, h), ledger)
        sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable
    assert "bad data" in sig.reason


@pytest.mark.asyncio
async def test_round_trip_on_a_normal_market_is_a_loss(cfg, http, ledger):
    """The headline claim, tested: buying and selling into the same pool loses."""
    cfg.strategy = "roundtrip"
    strat = RoundTripStrategy(cfg, MarketData(cfg, http), ledger)
    sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable
    assert sig.gross_bps < 0
    await http.aclose()


@pytest.mark.asyncio
async def test_round_trip_fires_only_on_real_edge(cfg, ledger):
    class Skewed(FakeChain):
        def handler(self, request):
            # Selling back pays 3% more than buying cost — a genuine dislocation.
            if "/quote" in str(request.url) and request.url.params["inputMint"] == SOL_MINT:
                self.dex_price = 103.0
            else:
                self.dex_price = 100.0
            return super().handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(Skewed(impact_bps=1.0).handler)) as h:
        strat = RoundTripStrategy(cfg, MarketData(cfg, h), ledger)
        sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert sig.action == "BUY"
    assert sig.edge_bps > 0
    assert sig.meta["roundtrip"] is True


@pytest.mark.asyncio
async def test_strategy_refuses_a_quote_asset_it_cannot_price(cfg, http, ledger):
    """BONK/JUP has no USD anchor — sizing it by guesswork would mis-size
    every order, so the strategy declines instead."""
    reg = cfg.registry
    cfg.pairs = parse_pairs("BONK/JUP", reg)
    strat = BasisStrategy(cfg, MarketData(cfg, http), ledger)
    sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable
    assert "cannot size in JUP" in sig.reason or "no reference price" in sig.reason
    await http.aclose()


@pytest.mark.asyncio
async def test_momentum_warms_up_before_trading(cfg, http, ledger):
    cfg.strategy = "momentum"
    strat = build_strategy(cfg, MarketData(cfg, http), ledger)
    sig = await strat.evaluate(cfg.pairs[0], 100.0)
    assert not sig.actionable and "warming up" in sig.reason
    await http.aclose()


# ------------------------------------------------------------------ execution
@pytest.mark.asyncio
async def test_paper_fill_is_pessimistic(cfg, http, ledger):
    md = MarketData(cfg, http)
    sol, usdc = cfg.registry.get("SOL"), cfg.registry.get("USDC")
    q = await md.dex_quote(usdc, sol, 100.0)

    class S:
        quote, in_token, out_token = q, usdc, sol

    res = await PaperExecutor(cfg, md).execute(S(), 100.0)
    quoted = sol.from_atoms(q.out_atoms)
    assert res.ok
    assert res.out_amount < quoted            # haircut applied, never in our favour
    assert res.fee_usd == pytest.approx(0.0205)
    await http.aclose()


# --------------------------------------------------------------------- engine
@pytest.mark.asyncio
async def test_engine_tick_disarmed_places_no_trades(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = FakeChain(dex_price=90.0, ref_price=100.0)   # screaming buy signal
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        await eng.tick()
        assert eng.ledger.trade_count() == 0
        assert eng.last_signals["SOL/USDC"].action == "BUY"
        eng.ledger.close()


@pytest.mark.asyncio
async def test_engine_tick_armed_fills_and_books_it(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = FakeChain(dex_price=90.0, ref_price=100.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        eng.arm("test")
        await eng.tick()
        assert eng.ledger.trade_count() == 1
        pos = eng.ledger.position("SOL/USDC")
        assert pos.quantity > 0
        assert pos.avg_cost == pytest.approx(90.0, rel=0.05)
        eng.ledger.close()


@pytest.mark.asyncio
async def test_engine_halts_on_daily_loss(tmp_path):
    cfg = make_cfg(tmp_path, max_daily_loss_usd=1.0)
    chain = FakeChain(dex_price=90.0, ref_price=100.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        eng.arm("test")
        eng.ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="BUY",
                               in_symbol="USDC", in_amount=100.0, out_symbol="SOL", out_amount=1.0,
                               price_usd=100.0, notional_usd=100.0, fee_usd=0.0)
        eng.ledger.record_fill(mode="paper", strategy="basis", pair="SOL/USDC", action="SELL",
                               in_symbol="SOL", in_amount=1.0, out_symbol="USDC", out_amount=50.0,
                               price_usd=50.0, notional_usd=50.0, fee_usd=0.0)
        await eng.tick()
        assert eng.risk.halted
        eng.ledger.close()


@pytest.mark.asyncio
async def test_engine_status_is_json_safe(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = FakeChain()
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        await eng.tick()
        json.dumps(eng.status(), default=str)
        eng.ledger.close()


@pytest.mark.asyncio
async def test_panic_then_resume_flow(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = FakeChain(dex_price=90.0, ref_price=100.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        eng.arm("test")
        eng.panic("test")
        ok, msg = eng.arm("test")
        assert not ok and "halted" in msg
        await eng.tick()
        assert eng.ledger.trade_count() == 0     # panic really stops orders
        eng.resume("test")
        ok, _ = eng.arm("test")
        assert ok
        await eng.tick()
        assert eng.ledger.trade_count() == 1
        eng.ledger.close()


@pytest.mark.asyncio
async def test_live_mode_requires_solders(tmp_path):
    cfg = make_cfg(tmp_path, mode="live", private_key="not-a-real-key")
    chain = FakeChain()
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as h:
        eng = TradingEngine(cfg, http=h)
        from crypto.execution import ExecutionError, LiveSolanaExecutor
        assert isinstance(eng.executor, LiveSolanaExecutor)
        with pytest.raises(ExecutionError):
            eng.executor._load_keypair()      # bad key or missing lib — both refuse
        eng.ledger.close()
