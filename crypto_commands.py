"""Discord control surface for the crypto trading engine.

Kept out of main.py so the shop bot has no hard dependency on it: main.py
imports this inside a try/except, and if anything here fails to load the rest
of the bot carries on untouched.

Who may drive it: the guild owner, plus any user id in CRYPTO_ADMIN_USER_IDS.
Deliberately NOT manage-guild staff — the ticket/pricing moderators have no
business arming something that spends the owner's wallet.
"""

import json
import traceback

import discord
from discord import app_commands

from crypto.config import load_config
from crypto.engine import TradingEngine
from crypto.ledger import Ledger
from crypto.markets import parse_pairs
from crypto.notifier import LogNotifier, MultiNotifier, Notifier
from crypto.quotes import MarketData

OK_COLOR = 0x57F287
WARN_COLOR = 0xFEE75C
ERR_COLOR = 0xED4245
INFO_COLOR = 0xC9DBE6
TRADE_COLOR = 0x5865F2

LEVEL_COLOR = {"info": INFO_COLOR, "trade": TRADE_COLOR, "warn": WARN_COLOR, "error": ERR_COLOR}


class DiscordNotifier(Notifier):
    """Pushes engine events into a channel. Never raises into the engine."""

    def __init__(self, bot, channel_id):
        self.bot = bot
        self.channel_id = str(channel_id or "").strip()

    async def notify(self, level, title, message, fields=None):
        if not self.channel_id:
            return
        try:
            ch = self.bot.get_channel(int(self.channel_id))
            if ch is None:
                ch = await self.bot.fetch_channel(int(self.channel_id))
        except Exception:
            return
        embed = discord.Embed(title=title, description=str(message)[:4000],
                              color=LEVEL_COLOR.get(level, INFO_COLOR))
        for k, v in list((fields or {}).items())[:20]:
            embed.add_field(name=str(k)[:256], value=str(v)[:1024], inline=True)
        try:
            await ch.send(embed=embed)
        except Exception:
            pass


class CryptoState:
    """Holds the one engine instance for the process."""

    def __init__(self, bot):
        self.bot = bot
        self.engine = None
        self.config_error = ""

    def build(self):
        if self.engine:
            return self.engine
        cfg = load_config()
        cfg.validate()
        notifier = MultiNotifier(LogNotifier(), DiscordNotifier(self.bot, cfg.alert_channel_id))
        self.engine = TradingEngine(cfg, notifier=notifier)
        return self.engine

    def get(self):
        """Engine or None — never raises, so commands can report the reason."""
        if self.engine:
            return self.engine
        try:
            return self.build()
        except Exception as e:
            self.config_error = str(e)
            return None


_state = None


def _is_operator(interaction, cfg=None) -> bool:
    uid = str(interaction.user.id)
    admins = (cfg.admin_user_ids if cfg else []) or []
    if uid in admins:
        return True
    try:
        if interaction.guild and interaction.guild.owner_id == interaction.user.id:
            return True
    except Exception:
        pass
    return False


def _deny(reason="Only the server owner or a configured crypto admin can do that."):
    return discord.Embed(title="Not allowed", description=reason, color=ERR_COLOR)


def _no_engine_embed(state):
    return discord.Embed(
        title="Trading engine unavailable",
        description=(state.config_error or "Engine is not configured.") +
                    "\n\nSet `CRYPTO_ENABLED=1` and the `CRYPTO_*` variables, then restart.",
        color=ERR_COLOR,
    )


def _money(x):
    return f"${x:,.2f}" if abs(x) >= 0.01 or x == 0 else f"${x:,.6f}"


def _status_embed(engine):
    s = engine.status()
    risk = s["risk"]
    running = "running" if s["running"] else "stopped"
    armed = "ARMED" if risk["armed"] else "disarmed"
    if risk["halted"]:
        armed = f"HALTED — {risk['halt_reason']}"
    color = ERR_COLOR if risk["halted"] else (OK_COLOR if (s["running"] and risk["armed"]) else WARN_COLOR)

    embed = discord.Embed(
        title=f"Crypto engine — {s['mode'].upper()} · {running} · {armed}",
        description=f"**{s['strategy']}** on {', '.join(s['pairs'])}",
        color=color,
    )
    today, alltime = s["today"], s["all_time"]
    embed.add_field(name="Today",
                    value=(f"PnL **{_money(today['pnl_usd'])}**\n"
                           f"{today['trades']} trades · {today['win_rate']:.0f}% win\n"
                           f"fees {_money(today['fees_usd'])}"), inline=True)
    embed.add_field(name="All time",
                    value=(f"PnL **{_money(alltime['pnl_usd'])}**\n"
                           f"{alltime['trades']} trades · {alltime['win_rate']:.0f}% win\n"
                           f"failed txs {alltime['failed_txs']}"), inline=True)
    embed.add_field(name="Limits",
                    value=(f"loss cap {_money(risk['daily_loss_limit'])}/day\n"
                           f"order {_money(engine.cfg.base_order_usd)}\n"
                           f"position cap {_money(engine.cfg.max_position_usd)}"), inline=True)

    if s["sol_usd"]:
        embed.add_field(name="SOL/USD", value=f"${s['sol_usd']:,.2f}", inline=True)
    embed.add_field(name="Ticks", value=f"{s['ticks']} ({s['errors']} errors)", inline=True)
    embed.add_field(name="Open value", value=_money(s["open_position_value_usd"]), inline=True)

    if s["signals"]:
        lines = "\n".join(f"• {v}" for v in list(s["signals"].values())[:5])
        embed.add_field(name="Latest signals", value=lines[:1024], inline=False)
    if s["preflight_notes"]:
        embed.add_field(name="Preflight warnings",
                        value="\n".join(f"• {n}" for n in s["preflight_notes"])[:1024], inline=False)
    if s["last_error"]:
        embed.add_field(name="Last error", value=str(s["last_error"])[:1024], inline=False)
    embed.set_footer(text="Paper mode books simulated fills against real quotes."
                          if s["mode"] == "paper" else "LIVE mode — real funds.")
    return embed


class CryptoGroup(app_commands.Group):
    def __init__(self, state):
        super().__init__(name="crypto", description="Automated crypto trading")
        self.state = state

    async def _guard(self, interaction):
        """Returns the engine, or None after replying with the reason."""
        engine = self.state.get()
        if not engine:
            await interaction.response.send_message(embed=_no_engine_embed(self.state), ephemeral=True)
            return None
        if not _is_operator(interaction, engine.cfg):
            await interaction.response.send_message(embed=_deny(), ephemeral=True)
            return None
        return engine

    # ------------------------------------------------------------- read-only
    @app_commands.command(name="status", description="Engine state, PnL and latest signals")
    async def status(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        await interaction.response.send_message(embed=_status_embed(engine), ephemeral=True)

    @app_commands.command(name="positions", description="Current inventory and cost basis")
    async def positions(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        rows = engine.ledger.positions()
        if not rows:
            await interaction.response.send_message(
                embed=discord.Embed(title="No positions", color=INFO_COLOR), ephemeral=True)
            return
        embed = discord.Embed(title="Positions", color=INFO_COLOR)
        for p in rows[:20]:
            sig = engine.last_signals.get(p.pair)
            mark = (sig.meta.get("ref_mid") or sig.meta.get("price") or 0.0) if sig else 0.0
            unreal = p.unrealized(mark) if mark else 0.0
            embed.add_field(
                name=p.pair,
                value=(f"qty **{p.quantity:,.6f}**\n"
                       f"avg cost {_money(p.avg_cost)}\n"
                       f"mark {_money(mark) if mark else '—'}\n"
                       f"unrealized **{_money(unreal)}**\n"
                       f"realized {_money(p.realized)}"),
                inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pnl", description="Realized PnL, fees and win rate")
    @app_commands.describe(day="UTC date as YYYY-MM-DD (default: all time)")
    async def pnl(self, interaction: discord.Interaction, day: str = None):
        engine = await self._guard(interaction)
        if not engine:
            return
        s = engine.ledger.stats(day)
        embed = discord.Embed(
            title=f"PnL — {day or 'all time'}",
            color=OK_COLOR if s["pnl_usd"] >= 0 else ERR_COLOR,
            description=(f"**{_money(s['pnl_usd'])}** net (already after fees)\n"
                         f"{s['trades']} trades · {s['wins']}W / {s['losses']}L "
                         f"({s['win_rate']:.1f}%)\n"
                         f"fees paid {_money(s['fees_usd'])} · failed txs {s['failed_txs']}"),
        )
        fills = engine.ledger.recent_fills(6)
        if fills:
            lines = []
            for f in fills:
                tag = "" if f["status"] == "filled" else f" [{f['status']}]"
                lines.append(f"`{f['action']:<4}` {f['pair']} {_money(f['notional_usd'])} "
                             f"→ {_money(f['realized_pnl'])}{tag}")
            embed.add_field(name="Recent fills", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="quote", description="Live executable quote for a pair")
    @app_commands.describe(pair="e.g. SOL/USDC", amount="Quote-token amount (default: your order size)")
    async def quote(self, interaction: discord.Interaction, pair: str = "SOL/USDC", amount: float = 0.0):
        engine = await self._guard(interaction)
        if not engine:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pairs = parse_pairs(pair, engine.cfg.registry)
            md = MarketData(engine.cfg, engine.http)
            sol = await md.sol_usd()
            p = pairs[0]
            amt = amount if amount > 0 else engine.cfg.base_order_usd
            q = await md.dex_quote(p.quote, p.base, amt)
            got = p.base.from_atoms(q.out_atoms)
            embed = discord.Embed(title=f"{p} — {amt:g} {p.quote.symbol}", color=INFO_COLOR)
            embed.add_field(name="You receive", value=f"**{got:,.6f}** {p.base.symbol}", inline=True)
            embed.add_field(name="Price", value=f"{amt / got:,.6f} {p.quote.symbol}", inline=True)
            embed.add_field(name="Price impact", value=f"{q.price_impact_bps:.2f} bps", inline=True)
            embed.add_field(name="Worst allowed fill",
                            value=f"{p.base.from_atoms(q.min_out_atoms):,.6f} {p.base.symbol}", inline=True)
            ref = await md.reference(p.base)
            if ref:
                embed.add_field(name=f"{ref.venue} reference",
                                value=f"bid {ref.bid:,.6f} / ask {ref.ask:,.6f}", inline=True)
            embed.set_footer(text=f"SOL/USD ${sol:,.2f}")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(title="Quote failed", description=str(e)[:1500], color=ERR_COLOR),
                ephemeral=True)

    @app_commands.command(name="config", description="Current settings (secrets hidden)")
    async def config(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        body = json.dumps(engine.cfg.redacted(), indent=2, default=str)
        await interaction.response.send_message(
            embed=discord.Embed(title="Crypto config", description=f"```json\n{body[:3900]}\n```",
                                color=INFO_COLOR), ephemeral=True)

    # -------------------------------------------------------------- lifecycle
    @app_commands.command(name="start", description="Start the loop (starts disarmed — no orders yet)")
    async def start(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await engine.start(arm=False)
        embed = discord.Embed(
            title="Engine started" if ok else "Could not start",
            description=(msg if not ok else
                         "Running **disarmed** — it will evaluate and log signals but place no orders.\n"
                         "Run `/crypto arm` when you want it to trade."),
            color=OK_COLOR if ok else ERR_COLOR)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop the loop")
    async def stop(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await engine.stop(f"stopped by {interaction.user}")
        await interaction.followup.send(
            embed=discord.Embed(title="Engine stopped" if ok else "Not running",
                                description=msg, color=OK_COLOR if ok else WARN_COLOR),
            ephemeral=True)

    @app_commands.command(name="arm", description="Allow the engine to place orders")
    async def arm(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        ok, msg = engine.arm(str(interaction.user))
        if ok and engine.cfg.is_live:
            desc = ("**LIVE — this now spends real funds.**\n"
                    f"Order size {_money(engine.cfg.base_order_usd)} · "
                    f"position cap {_money(engine.cfg.max_position_usd)} · "
                    f"daily loss cap {_money(engine.cfg.max_daily_loss_usd)}.\n"
                    "`/crypto panic` stops everything immediately.")
        elif ok:
            desc = "Paper mode — fills are simulated against real quotes. Nothing is spent."
        else:
            desc = msg
        await interaction.response.send_message(
            embed=discord.Embed(title="Armed" if ok else "Could not arm", description=desc,
                                color=(ERR_COLOR if not ok else (WARN_COLOR if engine.cfg.is_live else OK_COLOR))),
            ephemeral=True)

    @app_commands.command(name="disarm", description="Keep watching, stop placing orders")
    async def disarm(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        engine.disarm(str(interaction.user), "via /crypto disarm")
        await interaction.response.send_message(
            embed=discord.Embed(title="Disarmed",
                                description="Still evaluating; no orders will be placed.",
                                color=OK_COLOR), ephemeral=True)

    @app_commands.command(name="panic", description="Kill switch — halt all trading now")
    async def panic(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        engine.panic(str(interaction.user))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="HALTED",
                description=("No further orders will be placed. Open positions are **not** "
                             "liquidated — close them yourself if you want out.\n"
                             "`/crypto resume` clears the halt, then `/crypto arm` to trade again."),
                color=ERR_COLOR), ephemeral=True)

    @app_commands.command(name="resume", description="Clear a halt (leaves the engine disarmed)")
    async def resume(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        ok, msg = engine.resume(str(interaction.user))
        await interaction.response.send_message(
            embed=discord.Embed(title="Halt cleared", description=msg, color=OK_COLOR), ephemeral=True)

    @app_commands.command(name="signals", description="What the strategy last decided, and why")
    async def signals(self, interaction: discord.Interaction):
        engine = await self._guard(interaction)
        if not engine:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sol = await engine.market.sol_usd()
            lines = []
            for pair in engine.cfg.pairs:
                sig = await engine.strategy.evaluate(pair, sol)
                verdict = ""
                if sig.actionable:
                    d = engine.risk.check(sig, sol)
                    verdict = "\n   → **would trade**" if d.ok else f"\n   → blocked: {d.reason}"
                lines.append(f"• {sig.summary()}{verdict}")
            await interaction.followup.send(
                embed=discord.Embed(title="Live signals", description="\n".join(lines)[:4000],
                                    color=INFO_COLOR).set_footer(text=f"SOL/USD ${sol:,.2f}"),
                ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(title="Could not evaluate", description=str(e)[:1500],
                                    color=ERR_COLOR), ephemeral=True)


def setup_crypto(bot):
    """Register the /crypto command group. Safe to call once at import time."""
    global _state
    if _state is not None:
        return _state
    _state = CryptoState(bot)
    bot.tree.add_command(CryptoGroup(_state))
    return _state


async def start_crypto(bot):
    """Called from on_ready. Boots the engine if CRYPTO_ENABLED=1."""
    state = _state or setup_crypto(bot)
    try:
        cfg = load_config()
    except Exception as e:
        print(f"[Crypto] config error: {e}")
        state.config_error = str(e)
        return
    if not cfg.enabled:
        print("[Crypto] disabled (set CRYPTO_ENABLED=1 to enable)")
        return
    try:
        engine = state.build()
    except Exception as e:
        print(f"[Crypto] engine build failed: {e}")
        state.config_error = str(e)
        return
    print(f"[Crypto] ready — {cfg.mode} mode, strategy {cfg.strategy}, "
          f"pairs {[p.name for p in cfg.pairs]}")
    if not cfg.autostart:
        return
    try:
        ok, msg = await engine.start(arm=False)
        print(f"[Crypto] autostart: {msg}")
    except Exception as e:
        print(f"[Crypto] autostart failed: {e}")
        traceback.print_exc()


async def shutdown_crypto():
    if _state and _state.engine:
        try:
            await _state.engine.close()
        except Exception as e:
            print(f"[Crypto] shutdown error: {e}")
