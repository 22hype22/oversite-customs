import os
import io
import re
import json
import hashlib
import signal
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
import httpx
import aiohttp

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
BOT_ORDER_ID = os.getenv("BOT_ORDER_ID", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://prvqfjairnketwhmfshu.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBydnFmamFpcm5rZXR3aG1mc2h1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY4MDM2NDIsImV4cCI6MjA5MjM3OTY0Mn0.7IRfiBSkw5tM67fxYADmd8MQ619AjEb1v7exa2ZRth8")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or SUPABASE_ANON_KEY
SUPABASE_FN_URL = os.getenv("SUPABASE_FN_URL", f"{SUPABASE_URL}/functions/v1")
BOT_API = os.getenv("BOT_API_NAME", "utilities-bot-api")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://oversite.shop/bot-dashboard")

SERVER_NAME = os.getenv("SERVER_NAME", "Oversite Customs")
ACCENT = 0xC9DBE6
BOT_START_TIME = discord.utils.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID", "")
WELCOME_EMOJI_ID = int(os.getenv("WELCOME_EMOJI_ID", "1527943242115579905"))
MEMBER_COUNT_EMOJI_ID = int(os.getenv("MEMBER_COUNT_EMOJI_ID", "1474038929815507096"))
WELCOME_DASHBOARD_CHANNEL_ID = int(os.getenv("WELCOME_DASHBOARD_CHANNEL_ID", "1471291097040031916"))


def _split_ids(raw):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID", "")
TICKET_LOG_CHANNEL_ID = os.getenv("TICKET_LOG_CHANNEL_ID", "")
SUPPORT_ROLE_IDS = _split_ids(os.getenv("SUPPORT_ROLE_IDS"))
CREDIT_MANAGER_ROLE_IDS = _split_ids(os.getenv("CREDIT_MANAGER_ROLE_IDS"))

BUTTON_STYLE_MAP = {
    "primary": 1, "blurple": 1,
    "secondary": 2, "grey": 2, "gray": 2,
    "success": 3, "green": 3,
    "danger": 4, "red": 4,
    "link": 5,
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

welcome_config = {"enabled": True, "channel_id": WELCOME_CHANNEL_ID, "message": ""}
invite_config = {"channel_id": "", "components": [], "embeds": [], "messages": []}
ticket_config = {
    "category_id": TICKET_CATEGORY_ID,
    "support_role_ids": SUPPORT_ROLE_IDS,
    "log_channel_id": TICKET_LOG_CHANNEL_ID,
    "open_message": "",
    "ping_support": True,
    "one_per_user": True,
    # Rich panel (posted to a channel) + a list of ticket TYPES, each with its
    # own Open button (label/color) and its own opening message. types = [
    #   {id, name, button_label, button_style, open_components:[...]}, ... ]
    "panel_channel_id": "",
    "panel_components": [],
    "types": [],
    "panel_ref": None,
}

# Registry mapping a clicked Ticket/Ephemeral component back to the message the
# dashboard designed for it. Rebuilt from panel_components on every apply_config
# (and on boot), so it survives restarts.
ticket_msgs = {}   # key -> open_components (Ticket buttons/options)
eph_msgs = {}      # key -> open_components (Ephemeral buttons/options)
form_msgs = {}     # key -> open_components (Form buttons/options — collect {Question:} answers first)
form_titles = {}   # key -> modal title (the button/option label)
ticket_categories = {}  # key -> category name a Ticket/Form drops its channels into

def _msg_key(open_components, label=""):
    raw = json.dumps(open_components or [], sort_keys=True) + "|" + (label or "")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

def _comp_key(x):
    # Prefer the component's stable id (untouched by {user} substitution). Fall
    # back to a content hash only for components that carry no id (e.g. options).
    cid = x.get("id")
    if cid:
        return str(cid)[:64]
    return _msg_key(x.get("open_components"), x.get("label", ""))

def _register_ticket_components(comps):
    ticket_msgs.clear(); eph_msgs.clear(); form_msgs.clear(); form_titles.clear(); ticket_categories.clear()

    def _reg(x):
        oc = x.get("open_components") or []
        if "ticket" in x:
            k = _comp_key(x)
            ticket_msgs[k] = oc
            ticket_categories[k] = (x.get("category_name") or "").strip()
        elif "form" in x:
            k = _comp_key(x)
            form_msgs[k] = oc
            form_titles[k] = x.get("label") or "Application"
            ticket_categories[k] = (x.get("category_name") or "").strip()
        elif "ephemeral" in x:
            eph_msgs[_comp_key(x)] = oc
        # A Ticket/Ephemeral message can itself contain more Ticket/Ephemeral
        # buttons, so register the ones nested inside it too.
        if oc:
            walk(oc, 0)

    def walk(items, depth):
        if depth > 8:
            return
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "container":
                walk(c.get("children") or c.get("components") or [], depth + 1)
            elif t in ("buttonRow", "button_row", "buttons", "action_row"):
                for b in (c.get("buttons") or []):
                    if isinstance(b, dict):
                        _reg(b)
            elif t in ("select_menu", "select"):
                for o in (c.get("options") or []):
                    if isinstance(o, dict):
                        _reg(o)
            elif t == "section":
                b = c.get("button")
                if isinstance(b, dict):
                    _reg(b)

    walk(comps, 0)
    print(f"[Tickets] registry: {len(ticket_msgs)} ticket + {len(form_msgs)} form + {len(eph_msgs)} ephemeral messages")
    print(f"[Tickets] registry built: tickets={{{', '.join(f'{k}:{len(v)}' for k,v in ticket_msgs.items())}}} eph={{{', '.join(f'{k}:{len(v)}' for k,v in eph_msgs.items())}}}")
credits_config = {"manager_role_ids": CREDIT_MANAGER_ROLE_IDS, "currency_name": "credits", "log_channel_id": ""}
_credits_memory = {}
# Roblox OAuth verification config (from the dashboard "Verification" block).
roblox_config = {
    "channel_id": "",
    "verified_role_id": "",
    "set_nickname": True,
    "log_channel_id": "",
    "client_id": "",
    "client_secret": "",
    "components": [],
    "button_label": "Verify",
    "button_style": "primary",
}


def success_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0x57F287)


def error_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0xED4245)


def info_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=ACCENT)


def _fn_headers():
    return {
        "x-worker-token": WORKER_TOKEN,
        "Content-Type": "application/json",
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


_poll_session = None
_auth_warned = False


async def get_poll_session():
    global _poll_session
    if _poll_session is None or _poll_session.closed:
        _poll_session = aiohttp.ClientSession()
    return _poll_session


async def runtime_rpc(name, payload):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{name}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            print(f"[RPC] {name} failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[RPC] {name} error: {e}")
    return None


@bot.event
async def on_ready():
    print(f"{SERVER_NAME} bot online as {bot.user}")
    print(f"[Boot] bot {BOT_ORDER_ID} using worker token prefix {WORKER_TOKEN[:12] if WORKER_TOKEN else 'MISSING'} (len {len(WORKER_TOKEN) if WORKER_TOKEN else 0})")
    # Dropdown-in-modal (Close Order form) needs discord.py 2.6+ (discord.ui.Label).
    print(f"[Boot] discord.py {discord.__version__} | dropdown-in-modal supported: {hasattr(discord.ui, 'Label')}")

    if BOT_ORDER_ID and WORKER_TOKEN:
        for loop in (send_heartbeat, poll_configs, poll_shutdown, record_metrics_loop, poll_roblox_apply, poll_about_me):
            try:
                if not loop.is_running():
                    loop.start()
            except Exception as e:
                print(f"[Startup] loop start failed: {e}")
        await fire_online_status()

    try:
        await apply_bot_identity()
    except Exception as e:
        print(f"[Startup] identity failed: {e}")
    try:
        await apply_about_me()
    except Exception as e:
        print(f"[Startup] about-me failed: {e}")
    if not sync_identity.is_running():
        sync_identity.start()

    try:
        await load_all_configs()
    except Exception as e:
        print(f"[Startup] config load failed: {e}")

    if not update_status.is_running():
        update_status.start()
    await refresh_status()

    try:
        if os.getenv("SKIP_SYNC") == "1":
            print("Command sync skipped")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Sync error: {e}")


async def _order_policy():
    """Fetch the order's active flag + licensed server limit for bot-side
    guards. Returns (active, server_limit). Fails SAFE (active=True, limit=None)
    so an API hiccup never makes the bot abandon a legit server."""
    data = await runtime_rpc("runtime_bot_server_policy", {"_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID})
    if isinstance(data, dict):
        active = data.get("active", True)
        lim = data.get("limit")
        lim = int(lim) if isinstance(lim, (int, float)) or (isinstance(lim, str) and str(lim).isdigit()) else None
        return (active is not False), lim
    return True, None


@bot.event
async def on_guild_join(guild):
    """The bot was added to a server. It may only STAY if the owner is within
    their licensed server count (add more via 'Add to another server' in the
    dashboard). Otherwise it posts a short note and leaves. Fails SAFE: it only
    leaves on an explicit over-limit / inactive answer, never on an API error."""
    print(f"[Guild] joined {guild.id} ({guild.name}) — {guild.member_count} members")
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return

    # Register the join so it shows in the dashboard's server list (best effort).
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/guild-join",
            headers=_fn_headers(),
            json={
                "bot_id": BOT_ORDER_ID,
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "member_count": guild.member_count or 0,
            },
        )
    except Exception:
        pass

    active, limit = await _order_policy()
    over_limit = isinstance(limit, int) and limit >= 0 and len(bot.guilds) > limit
    if active and not over_limit:
        try:
            await cache_roles(guild.id)
            await cache_channels(guild.id)
        except Exception:
            pass
        return

    # Not licensed for this server — explain, then leave.
    reason = "the owner's plan is inactive" if not active else "this exceeds the owner's licensed server count"
    print(f"[Guild] {guild.id} not allowed ({reason}) — leaving (limit={limit}, in={len(bot.guilds)})")
    try:
        target = guild.system_channel
        if target is None or not target.permissions_for(guild.me).send_messages:
            target = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if target is not None:
            await target.send(embed=error_embed(
                "This bot is licensed per server",
                "Oversite Customs only runs in servers the owner has added through "
                "their **Oversite dashboard** (Add to another server). This server "
                "isn't covered by their plan, so I'm leaving. Ask the owner to add "
                "it from the dashboard, then re-invite.",
            ))
    except Exception:
        pass
    try:
        await guild.leave()
    except Exception as e:
        print(f"[Guild] leave failed: {e}")


@bot.event
async def on_guild_remove(guild):
    """Bot left / was kicked — free the server slot on the backend."""
    print(f"[Guild] removed from {guild.id} ({guild.name})")
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/guild-leave",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild.id)},
        )
    except Exception as e:
        print(f"[Guild] guild-leave report failed: {e}")


@bot.event
async def on_member_join(member):
    await refresh_status()
    components = invite_config.get("components") or []
    embeds_data = invite_config.get("embeds") or []
    if components or embeds_data:
        ch_id = invite_config.get("channel_id") or welcome_config.get("channel_id") or ""
        if ch_id:
            channel = member.guild.get_channel(int(ch_id))
            if channel:
                if components:
                    rendered = _render_invite_components(components, member)
                    await send_v2_message(channel, rendered)
                else:
                    rendered = _render_invite_components(embeds_data, member)
                    embeds = [build_embed(e) for e in rendered][:10]
                    try:
                        if embeds:
                            await channel.send(embeds=embeds)
                        for m in (invite_config.get("messages") or []):
                            await channel.send(_sub_placeholders(m, member))
                    except Exception as e:
                        print(f"[Invite] send failed: {e}")
        return
    if not welcome_config.get("enabled", True):
        return
    ch_id = welcome_config.get("channel_id") or ""
    if not ch_id:
        return
    channel = member.guild.get_channel(int(ch_id))
    if not channel:
        return
    await send_welcome(channel, member)


@bot.event
async def on_member_remove(member):
    await refresh_status()


_EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z][a-zA-Z0-9_]*)(?:~\d+)?:")
# A complete custom emoji already written out: <:name:id> or <a:name:id>.
_FULL_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")


def _resolve_emoji_shortcodes(text, guild):
    if ":" not in text or not guild:
        return text
    lookup = {e.name.lower(): e for e in guild.emojis}
    if not lookup:
        return text

    # Stash any already-complete custom emojis so we don't rewrite the `:name:`
    # inside <:name:id> — doing so leaves the raw emoji id dangling next to the
    # rendered emoji (e.g. "🔥 1527943242115579905>").
    saved = []

    def _stash(m):
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    protected = _FULL_EMOJI_RE.sub(_stash, text)

    def repl(match):
        emoji = lookup.get(match.group(1).lower())
        if emoji is None:
            return match.group(0)
        return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"

    resolved = _EMOJI_SHORTCODE_RE.sub(repl, protected)
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], resolved)


def _sub_placeholders(text, member):
    if not isinstance(text, str):
        return text
    g = member.guild
    count = str(g.member_count or 0)
    bot_count = str(sum(1 for m in g.members if m.bot))
    human_count = str(sum(1 for m in g.members if not m.bot)) if g.members else count
    boosts = str(g.premium_subscription_count or 0)
    boost_level = str(getattr(g, "premium_tier", 0) or 0)
    channel_count = str(len(g.channels))
    role_count = str(len(g.roles))
    repl = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": g.name,
        "{member_count}": count, "{members}": count, "{count}": count,
        "{player count}": count, "{player_count}": count,
        "{human_count}": human_count, "{humans}": human_count,
        "{bot_count}": bot_count, "{bot count}": bot_count, "{bots}": bot_count,
        "{boosts}": boosts, "{boost_count}": boosts,
        "{total server boosts}": boosts, "{server_boosts}": boosts,
        "{boost_level}": boost_level, "{boost_tier}": boost_level,
        "{channel_count}": channel_count, "{channels}": channel_count,
        "{role_count}": role_count, "{roles}": role_count,
        "{emoji}": f"<:e:{WELCOME_EMOJI_ID}>",
    }
    for token, value in repl.items():
        text = text.replace(token, value)
    return _resolve_emoji_shortcodes(text, member.guild)


def _render_guild_text(text, guild):
    """Resolve :emoji: shortcodes and {count}-style placeholders for text posted
    to a channel (no specific member). Used everywhere a panel/embed renders
    text so custom emojis and variables work every time."""
    if not isinstance(text, str) or not text:
        return text
    if guild is not None and "{" in text:
        count = str(getattr(guild, "member_count", 0) or 0)
        members = list(getattr(guild, "members", []) or [])
        bot_count = str(sum(1 for m in members if m.bot)) if members else "0"
        human_count = str(sum(1 for m in members if not m.bot)) if members else count
        boosts = str(getattr(guild, "premium_subscription_count", 0) or 0)
        boost_level = str(getattr(guild, "premium_tier", 0) or 0)
        repl = {
            "{server}": guild.name,
            "{member_count}": count, "{members}": count, "{count}": count,
            "{player count}": count, "{player_count}": count,
            "{human_count}": human_count, "{humans}": human_count,
            "{bot_count}": bot_count, "{bot count}": bot_count, "{bots}": bot_count,
            "{boosts}": boosts, "{boost_count}": boosts,
            "{total server boosts}": boosts, "{server_boosts}": boosts,
            "{boost_level}": boost_level, "{boost_tier}": boost_level,
            "{channel_count}": str(len(guild.channels)), "{channels}": str(len(guild.channels)),
            "{role_count}": str(len(guild.roles)), "{roles}": str(len(guild.roles)),
        }
        for token, value in repl.items():
            text = text.replace(token, value)
    return _resolve_emoji_shortcodes(text, guild)


_INVITE_TEXT_KEYS = {"text", "content", "label", "placeholder", "title", "description", "name", "value"}


def _render_invite_components(components, member):
    def walk(node):
        if isinstance(node, dict):
            return {
                k: _sub_placeholders(v, member) if k in _INVITE_TEXT_KEYS and isinstance(v, str) else walk(v)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(components)


async def send_welcome(channel, member):
    emoji = f"<:e:{WELCOME_EMOJI_ID}>"
    body = welcome_config.get("message") or f"{emoji} Welcome {member.mention} to **{SERVER_NAME}** — glad to have you."
    body = body.replace("{user}", member.mention).replace("{server}", SERVER_NAME).replace("{emoji}", emoji)
    view = discord.ui.View()
    count_btn = discord.ui.Button(
        label=str(member.guild.member_count),
        style=discord.ButtonStyle.secondary,
        emoji=discord.PartialEmoji(name="members", id=MEMBER_COUNT_EMOJI_ID),
        disabled=True,
    )
    dash_btn = discord.ui.Button(
        label="Dashboard",
        style=discord.ButtonStyle.link,
        url=f"https://discord.com/channels/{member.guild.id}/{WELCOME_DASHBOARD_CHANNEL_ID}",
    )
    view.add_item(count_btn)
    view.add_item(dash_btn)
    try:
        await channel.send(content=body, view=view)
    except Exception as e:
        print(f"[Welcome] send failed: {e}")


async def refresh_status():
    total = sum((g.member_count or 0) for g in bot.guilds)
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name=f"Overseeing {total} members"),
        )
    except Exception as e:
        print(f"[Status] update failed: {e}")


@tasks.loop(minutes=10)
async def update_status():
    await refresh_status()


@tasks.loop(seconds=20)
async def poll_about_me():
    # Apply dashboard About Me edits within ~20s (only PATCHes when it changed,
    # so this is cheap and never hits Discord unless the text is new).
    await apply_about_me()


@poll_about_me.before_loop
async def before_poll_about_me():
    await bot.wait_until_ready()


@update_status.before_loop
async def before_update_status():
    await bot.wait_until_ready()


def has_any_role(member, role_ids):
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    ids = {str(r.id) for r in member.roles}
    return bool(ids & set(str(x) for x in role_ids))


credits_group = app_commands.Group(name="credits", description="Manage member credits")


@credits_group.command(name="add", description="Give credits to a member")
@app_commands.describe(member="Member to credit", amount="How many credits", reason="Why")
async def credits_add(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000000], reason: str = "No reason provided"):
    if not has_any_role(interaction.user, credits_config.get("manager_role_ids", [])):
        await interaction.response.send_message(embed=error_embed("No permission", "You can't manage credits."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    total = await credits_change(interaction.guild_id, member.id, amount, reason, interaction.user.id)
    await log_credit_action(interaction.guild, f"{interaction.user.mention} gave **{amount}** credits to {member.mention} — {reason}")
    await interaction.followup.send(embed=success_embed("Credits added", f"{member.mention} now has **{total}** credits."), ephemeral=True)


@credits_group.command(name="remove", description="Remove credits from a member")
@app_commands.describe(member="Member to debit", amount="How many credits", reason="Why")
async def credits_remove(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000000], reason: str = "No reason provided"):
    if not has_any_role(interaction.user, credits_config.get("manager_role_ids", [])):
        await interaction.response.send_message(embed=error_embed("No permission", "You can't manage credits."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    total = await credits_change(interaction.guild_id, member.id, -amount, reason, interaction.user.id)
    await log_credit_action(interaction.guild, f"{interaction.user.mention} removed **{amount}** credits from {member.mention} — {reason}")
    await interaction.followup.send(embed=success_embed("Credits removed", f"{member.mention} now has **{total}** credits."), ephemeral=True)


@credits_group.command(name="view", description="View a member's credits and history")
@app_commands.describe(member="Member to look up (leave blank for yourself)")
async def credits_view(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    member = member or interaction.user
    total, entries = await credits_lookup(interaction.guild_id, member.id)
    embed = info_embed(f"{member.display_name}'s credits", f"**{total}** {credits_config.get('currency_name', 'credits')} total")
    embed.set_thumbnail(url=member.display_avatar.url)
    if entries:
        lines = []
        for e in entries[:15]:
            amt = e.get("amount", 0)
            sign = "+" if amt >= 0 else ""
            by = e.get("granted_by")
            by_txt = f"<@{by}>" if by else "system"
            reason = e.get("reason", "")
            lines.append(f"`{sign}{amt}` by {by_txt} — {reason}")
        embed.add_field(name="History", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="History", value="No credit history yet.", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def credits_change(guild_id, user_id, amount, reason, granted_by):
    result = await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "add",
        "_payload": {"guild_id": str(guild_id), "user_id": str(user_id), "amount": amount, "reason": reason, "granted_by": str(granted_by)},
    })
    if isinstance(result, dict) and "total" in result:
        return result["total"]
    key = (str(guild_id), str(user_id))
    mem = _credits_memory.setdefault(key, {"total": 0, "entries": []})
    mem["total"] += amount
    mem["entries"].insert(0, {"amount": amount, "reason": reason, "granted_by": str(granted_by)})
    return mem["total"]


async def credits_lookup(guild_id, user_id):
    result = await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "balance",
        "_payload": {"guild_id": str(guild_id), "user_id": str(user_id)},
    })
    if isinstance(result, dict) and "total" in result:
        return result.get("total", 0), result.get("entries", []) or []
    mem = _credits_memory.get((str(guild_id), str(user_id)), {"total": 0, "entries": []})
    return mem["total"], mem["entries"]


async def log_credit_action(guild, text):
    ch_id = credits_config.get("log_channel_id") or ""
    if not ch_id or not guild:
        return
    channel = guild.get_channel(int(ch_id))
    if channel:
        try:
            await channel.send(embed=info_embed("Credit log", text))
        except Exception:
            pass


bot.tree.add_command(credits_group)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Form submits arrive as modal_submit interactions (not component). Handle
    # ours here; leave every other modal (Close Order, etc.) to discord.py's own
    # Modal dispatch by returning. This fires regardless of restarts, so forms
    # keep working across redeploys.
    if interaction.type == discord.InteractionType.modal_submit:
        cid = (interaction.data or {}).get("custom_id", "")
        if cid.startswith("ticketform:"):
            await handle_ticket_form_submit(interaction, cid.split(":", 1)[1])
        return
    if interaction.type != discord.InteractionType.component:
        return
    cid = (interaction.data or {}).get("custom_id", "")
    if cid.startswith(("ticket_msg:", "ticket_form:", "eph:", "ticket_cat:")) or cid in ("ticket_select", "ticket_open"):
        print(f"[Tickets] interaction cid={cid!r} values={(interaction.data or {}).get('values')}")
    if cid == "ticket_select":
        values = (interaction.data or {}).get("values") or []
        if values:
            v = values[0]
            if v.startswith("ticket_msg:"):
                mk = v.split(":", 1)[1]
                await open_ticket(interaction, v, open_comps_override=ticket_msgs.get(mk), category_name_override=ticket_categories.get(mk))
            elif v.startswith("ticket_form:"):
                await open_ticket_form(interaction, v.split(":", 1)[1])
            elif v.startswith("eph:"):
                await show_ephemeral(interaction, v.split(":", 1)[1])
            elif v.startswith("ch:") or v.startswith("url:"):
                try:
                    await interaction.response.defer(ephemeral=True)
                except Exception:
                    pass
            else:
                await open_ticket(interaction, v)
    elif cid.startswith("ticket_msg:"):
        mk = cid.split(":", 1)[1]
        await open_ticket(interaction, cid, open_comps_override=ticket_msgs.get(mk), category_name_override=ticket_categories.get(mk))
    elif cid.startswith("ticket_form:"):
        await open_ticket_form(interaction, cid.split(":", 1)[1])
    elif cid.startswith("eph:"):
        await show_ephemeral(interaction, cid.split(":", 1)[1])
    elif cid.startswith("ticket_cat:"):
        await open_ticket(interaction, cid.split(":", 1)[1])
    elif cid == "ticket_open":
        await open_ticket(interaction, "support")
    elif cid == "ticket_claim":
        await ticket_claim_toggle(interaction, True)
    elif cid == "ticket_unclaim":
        await ticket_claim_toggle(interaction, False)
    elif cid == "ticket_close":
        await ticket_close_prompt(interaction)
    elif cid == "ticket_closetype":
        values = (interaction.data or {}).get("values") or []
        mode = values[0] if values else "instant"
        await interaction.response.send_modal(CloseReasonModal(mode))
    elif cid == "ticket_close_confirm":
        await close_ticket(interaction)
    elif cid == "roblox_verify":
        await start_roblox_verify(interaction)


def _ticket_topic(opener_id, category, base=""):
    return f"ticket|{opener_id}|{category}|{base}"


def _san_name(x):
    x = re.sub(r"<[^>]+>", "", str(x or ""))
    x = x.lower().replace(" ", "-")
    x = re.sub(r"[^a-z0-9\-]", "", x)
    x = re.sub(r"-+", "-", x).strip("-")
    return x[:40] or "ticket"


def _ticket_first_word(open_comps):
    def find_text(items):
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t in ("text", "text_display", "section"):
                txt = c.get("text") or c.get("content") or c.get("title") or ""
                if str(txt).strip():
                    return str(txt)
            if t == "container":
                r = find_text(c.get("children") or c.get("components") or [])
                if r:
                    return r
        return ""
    txt = re.sub(r"<[^>]+>", "", find_text(open_comps))
    txt = re.sub(r"[*_`~>#|:\-]", " ", txt)
    words = [w for w in txt.split() if w]
    return words[0] if words else ""


_QUESTION_RE = re.compile(r"\{Question:\s*(.*?)\}", re.IGNORECASE)


def _existing_ticket_for(guild, user_id):
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if topic.startswith("ticket|") and topic.split("|")[1] == str(user_id):
            return ch
    return None


def _clean_label(s):
    """Strip markdown emphasis so a {Question: **Server Name:**} token shows a
    clean 'Server Name:' label in the modal instead of literal asterisks."""
    return re.sub(r"[*_`~]", "", s or "").strip()


def _parse_questions(open_comps):
    """Ordered, de-duplicated list of {Question: LABEL} labels in a design (max 5)."""
    raw = json.dumps(open_comps or [])
    seen = []
    for m in _QUESTION_RE.finditer(raw):
        lbl = (m.group(1) or "").strip()
        if lbl and lbl not in seen:
            seen.append(lbl)
    return seen[:5]


def _form_input_style(label):
    l = (label or "").lower()
    if any(k in l for k in ("descri", "about", "why", "reason", "detail", "explain", "tell", "message")):
        return 2  # paragraph
    return 1  # short


def _collect_modal_values(components):
    """Flatten a modal_submit component tree into {custom_id: value}. Handles
    Label-wrapped inputs (type 18 -> component), action rows (type 1), and bare
    text inputs (type 4)."""
    vals = {}
    for row in components or []:
        if not isinstance(row, dict):
            continue
        inner = row.get("component")
        if isinstance(inner, dict) and inner.get("custom_id"):
            vals[inner["custom_id"]] = inner.get("value", "") or ""
        for c in (row.get("components") or []):
            if isinstance(c, dict) and c.get("custom_id"):
                vals[c["custom_id"]] = c.get("value", "") or ""
        if row.get("type") == 4 and row.get("custom_id"):
            vals[row["custom_id"]] = row.get("value", "") or ""
    return vals


def _apply_answers(open_comps, mapping):
    """Replace each {Question: LABEL} token with '**LABEL** answer'."""
    raw = json.dumps(open_comps or [])

    def repl(m):
        label = (m.group(1) or "").strip()
        answer = mapping.get(label, "")
        clean = _clean_label(label)
        out = f"**{clean}** {answer}".strip() if answer else f"**{clean}**"
        return json.dumps(out)[1:-1]  # JSON-escape (we're inside a string literal)

    return json.loads(_QUESTION_RE.sub(repl, raw))


async def open_ticket_form(interaction, key):
    """A Form button/option: pop a modal to collect {Question:} answers, then
    open the ticket with those answers filled into the designed message."""
    open_comps = form_msgs.get(key) or []
    questions = _parse_questions(open_comps)
    if not questions:
        # No questions defined — behave exactly like a Ticket button.
        await open_ticket(interaction, f"ticket_form:{key}", open_comps_override=open_comps)
        return

    guild = interaction.guild
    if guild and ticket_config.get("one_per_user", True):
        existing = _existing_ticket_for(guild, interaction.user.id)
        if existing:
            try:
                await interaction.response.send_message(
                    embed=error_embed("Ticket already open", f"You already have an open ticket: {existing.mention}"),
                    ephemeral=True,
                )
            except Exception:
                pass
            return

    components = []
    for i, q in enumerate(questions):
        components.append({
            "type": 18,  # Label — carries the field label
            "label": (_clean_label(q) or q)[:45],
            "component": {
                "type": 4,  # text input (no own label when inside a Label)
                "custom_id": f"q{i}",
                "style": _form_input_style(q),
                "required": True,
                "max_length": 1000,
            },
        })
    data = {
        "title": (form_titles.get(key) or "Application")[:45],
        "custom_id": f"ticketform:{key}",
        "components": components,
    }
    try:
        route = discord.http.Route(
            "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id, interaction_token=interaction.token,
        )
        await bot.http.request(route, json={"type": 9, "data": data})
    except Exception as e:
        print(f"[Ticket] form modal failed: {e}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", "Please try again."), ephemeral=True)
        except Exception:
            pass


async def handle_ticket_form_submit(interaction, key):
    # Acknowledge the modal IMMEDIATELY (before any work) so Discord never shows
    # "Something went wrong" — then build the ticket and follow up.
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception as e:
        print(f"[Ticket] form submit defer failed: {e}")
    try:
        open_comps = form_msgs.get(key) or []
        labels = _parse_questions(open_comps)
        vals = _collect_modal_values((interaction.data or {}).get("components"))
        mapping = {lbl: (vals.get(f"q{i}") or "").strip() for i, lbl in enumerate(labels)}
        substituted = _apply_answers(open_comps, mapping)
        await open_ticket(interaction, f"ticket_form:{key}", open_comps_override=substituted,
                          category_name_override=ticket_categories.get(key), already_responded=True)
    except Exception as e:
        import traceback
        print(f"[Ticket] form submit failed: {e}\n{traceback.format_exc()}")
        try:
            await interaction.followup.send(embed=error_embed("Couldn't open ticket", "Something went wrong creating your ticket. Please try again."), ephemeral=True)
        except Exception:
            pass


_category_locks = {}


async def _get_or_create_category(guild, name):
    """Find a category by name (case-insensitive), creating it only if none
    exists. A per-name lock + re-check makes concurrent ticket opens reuse the
    same category instead of racing to create duplicate 'ELS' categories."""
    name = (name or "").strip()
    if not name:
        return None
    target = name.lower()

    def _find():
        for cat in guild.categories:
            if cat.name.strip().lower() == target:
                return cat
        return None

    existing = _find()
    if existing:
        return existing

    key = (guild.id, target)
    lock = _category_locks.get(key)
    if lock is None:
        lock = _category_locks[key] = asyncio.Lock()
    async with lock:
        # Another open may have created it while we waited for the lock.
        existing = _find()
        if existing:
            return existing
        try:
            return await guild.create_category(name=name[:100], reason="Ticket category")
        except Exception as e:
            print(f"[Tickets] category create failed for {name!r}: {e}")
            return None


async def open_ticket(interaction, category, open_comps_override=None, category_name_override=None, already_responded=False):
    guild = interaction.guild
    if not guild:
        return
    if not already_responded:
        await interaction.response.defer(ephemeral=True)

    if ticket_config.get("one_per_user", True):
        for ch in guild.text_channels:
            topic = ch.topic or ""
            if topic.startswith("ticket|") and topic.split("|")[1] == str(interaction.user.id):
                await interaction.followup.send(embed=error_embed("Ticket already open", f"You already have an open ticket: {ch.mention}"), ephemeral=True)
                return

    # Per-Ticket/Form category (by name, created on demand) wins; otherwise fall
    # back to the globally configured category id.
    category_channel = None
    if category_name_override:
        category_channel = await _get_or_create_category(guild, category_name_override)
    if category_channel is None:
        cat_id = ticket_config.get("category_id") or ""
        if cat_id:
            category_channel = guild.get_channel(int(cat_id))

    support_roles = []
    for rid in ticket_config.get("support_role_ids", []):
        role = guild.get_role(int(rid))
        if role:
            support_roles.append(role)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    for role in support_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    tdef = next((t for t in ticket_config.get("types", []) if t.get("id") == category), None)
    open_comps = open_comps_override if open_comps_override is not None else ((tdef.get("open_components") if tdef else None) or [])
    type_name = (tdef.get("name") if tdef else None) or str(category).replace("_", " ").title()
    first_word = _ticket_first_word(open_comps) or (type_name.split()[0] if type_name.split() else "ticket")
    ticket_base = f"{_san_name(interaction.user.name)}-{_san_name(first_word)}".strip("-") or _san_name(interaction.user.name)
    base_name = f"\U0001F534\u30FB{ticket_base}"[:90]
    try:
        channel = await guild.create_text_channel(
            name=base_name,
            category=category_channel if isinstance(category_channel, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=_ticket_topic(interaction.user.id, category, ticket_base),
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=error_embed("Couldn't open ticket", "I'm missing the Manage Channels permission."), ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(embed=error_embed("Couldn't open ticket", str(e)), ephemeral=True)
        return

    ping = ""
    if ticket_config.get("ping_support", True) and support_roles:
        ping = " ".join(r.mention for r in support_roles)

    # Only ping support roles (when enabled). The opener isn't pinged — they
    # already have access and get an ephemeral link to the channel.
    content = ping or None

    # (ticket type + opening components resolved above for the channel name)
    sent_rich = False
    if open_comps:
        try:
            def _js(s):
                return json.dumps(str(s))[1:-1]
            raw = json.dumps(open_comps)
            raw = raw.replace("{user}", _js(interaction.user.mention)).replace("{username}", _js(interaction.user.display_name))
            comps = json.loads(raw)
            close_row = {"type": "buttonRow", "buttons": [{"label": "Claim", "style": "success", "__ticket_claim": True}, {"label": "Close Order", "style": "danger", "__ticket_close": True}]}
            panel = [dict(c) for c in comps]
            container_idxs = [i for i, c in enumerate(panel) if c.get("type") == "container"]
            if container_idxs:
                i = container_idxs[-1]
                panel[i] = dict(panel[i])
                panel[i]["children"] = list(panel[i].get("children") or []) + [close_row]
            else:
                panel.append(close_row)
            # Ping first (plain message) so the opener + support actually get notified,
            # then the rich Components V2 message (which can't carry a pinging content).
            if content:
                try:
                    await channel.send(content=content)
                except Exception:
                    pass
            # Allow role + user mentions inside the ticket message to actually
            # ping (e.g. a @Livery Designer role written into the design).
            sent_rich = bool(await send_v2_message(channel, panel, allowed_mentions={"parse": ["users", "roles"]}))
        except Exception as e:
            print(f"[Tickets] rich open message failed: {e}")
            sent_rich = False

    if not sent_rich:
        open_msg = ticket_config.get("open_message") or f"Thanks {interaction.user.mention}, a member of the team will be with you shortly."
        open_msg = open_msg.replace("{user}", interaction.user.mention)
        embed = info_embed(f"{type_name} ticket", open_msg)
        embed.set_footer(text=f"Opened by {interaction.user}")

        close_view = discord.ui.View(timeout=None)
        close_view.add_item(discord.ui.Button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim"))
        close_view.add_item(discord.ui.Button(label="Close Order", style=discord.ButtonStyle.danger, custom_id="ticket_close", emoji="🔒"))

        await channel.send(content=content, embed=embed, view=close_view)
    await record_ticket(guild.id, channel.id, interaction.user.id, category, "open")
    await interaction.followup.send(embed=success_embed("Ticket opened", f"Your ticket is ready: {channel.mention}"), ephemeral=True)


async def show_ephemeral(interaction, key):
    comps = eph_msgs.get(key)
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass
    if not comps:
        try:
            await interaction.followup.send(embed=info_embed("Nothing here", "This option isn't set up yet."), ephemeral=True)
        except Exception:
            pass
        return

    def _js(x):
        return json.dumps(str(x))[1:-1]
    try:
        raw = json.dumps(comps)
        raw = raw.replace("{user}", _js(interaction.user.mention)).replace("{username}", _js(interaction.user.display_name))
        comps2 = json.loads(raw)
    except Exception:
        comps2 = comps
    ok = await send_v2_message(interaction.channel, comps2, interaction=interaction, ephemeral=True)
    if not ok:
        try:
            await interaction.followup.send(embed=info_embed("Note", "Couldn't render this message."), ephemeral=True)
        except Exception:
            pass


async def _do_close(channel, guild, closer, reason=""):
    topic = getattr(channel, "topic", "") or ""
    parts = topic.split("|")
    opener_id = parts[1] if len(parts) > 1 else ""
    category = parts[2] if len(parts) > 2 else "support"
    transcript = await build_transcript(channel)
    log_id = ticket_config.get("log_channel_id") or ""
    opener = guild.get_member(int(opener_id)) if opener_id.isdigit() else None
    if log_id:
        log_channel = guild.get_channel(int(log_id))
        if log_channel:
            desc = f"**Category:** {category}\n**Opened by:** {opener.mention if opener else opener_id}\n**Closed by:** {closer.mention}"
            if reason:
                desc += f"\n**Reason:** {reason}"
            try:
                await log_channel.send(embed=info_embed("Ticket closed", desc), file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{channel.name}.txt"))
            except Exception as e:
                print(f"[Ticket] log failed: {e}")
    await record_ticket(guild.id, channel.id, opener_id, category, "closed")
    await asyncio.sleep(2)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except Exception as e:
        print(f"[Ticket] delete failed: {e}")


async def close_ticket(interaction):
    channel = interaction.channel
    topic = getattr(channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        await interaction.response.send_message(embed=error_embed("Not a ticket", "This channel isn't a ticket."), ephemeral=True)
        return
    opener_id = topic.split("|")[1] if len(topic.split("|")) > 1 else ""
    is_support = has_any_role(interaction.user, ticket_config.get("support_role_ids", []))
    is_opener = str(interaction.user.id) == opener_id
    if not (is_support or is_opener or interaction.user.guild_permissions.manage_channels):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff or the opener can close this."), ephemeral=True)
        return
    await interaction.response.send_message(embed=info_embed("Closing order", "Saving transcript and closing\u2026"))
    await _do_close(channel, interaction.guild, interaction.user)


def _is_ticket_staff(member):
    try:
        if member.guild_permissions.manage_channels:
            return True
    except Exception:
        pass
    return has_any_role(member, ticket_config.get("support_role_ids", []))


def _toggle_claim_in_components(comps, claimed):
    for c in (comps or []):
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("components"), list):
            _toggle_claim_in_components(c["components"], claimed)
        if c.get("type") == 2 and c.get("custom_id") in ("ticket_claim", "ticket_unclaim"):
            c.pop("emoji", None)
            if claimed:
                c["custom_id"], c["label"], c["style"] = "ticket_unclaim", "Unclaim", 2
            else:
                c["custom_id"], c["label"], c["style"] = "ticket_claim", "Claim", 3


async def ticket_claim_toggle(interaction, claimed):
    member = interaction.user
    if not _is_ticket_staff(member):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can claim orders."), ephemeral=True)
        return
    channel, msg = interaction.channel, interaction.message
    if claimed:
        await interaction.response.send_message(embed=info_embed("Order claimed", f"{member.mention} claimed this order."))
    else:
        await interaction.response.send_message(embed=info_embed("Order unclaimed", f"{member.mention} unclaimed this order."))
    try:
        raw = await bot.http.get_message(channel.id, msg.id)
        comps = raw.get("components", []) or []
        _toggle_claim_in_components(comps, claimed)
        route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=channel.id, message_id=msg.id)
        await bot.http.request(route, json={"components": comps, "flags": raw.get("flags", 0)})
    except Exception as e:
        print(f"[Tickets] claim toggle failed: {e}")
    # Rename + reorder: on claim, go green + claimer and jump to the TOP of the
    # category (saving the old slot in the topic). On unclaim, go back to red +
    # opener-firstword and drop back to where it was.
    try:
        parts = (getattr(channel, "topic", "") or "").split("|")
        opener_id = parts[1] if len(parts) > 1 else ""
        cat = parts[2] if len(parts) > 2 else "support"
        base = parts[3] if len(parts) > 3 and parts[3] else _san_name(getattr(channel, "name", "ticket"))
        if claimed:
            saved_pos = getattr(channel, "position", 0)
            new_name = f"\U0001F7E2\u30FB{_san_name(member.name)}"[:90]
            new_topic = f"ticket|{opener_id}|{cat}|{base}|{saved_pos}"
            await channel.edit(name=new_name, topic=new_topic, reason=f"Ticket claimed by {member}")
            try:
                await channel.move(beginning=True, category=channel.category, sync_permissions=False, reason="Claimed ticket to top")
            except Exception as e:
                print(f"[Tickets] move-to-top failed: {e}")
        else:
            saved_pos = None
            if len(parts) > 4 and parts[4].strip().lstrip("-").isdigit():
                saved_pos = int(parts[4])
            new_name = f"\U0001F534\u30FB{base}"[:90]
            new_topic = f"ticket|{opener_id}|{cat}|{base}"
            await channel.edit(name=new_name, topic=new_topic, reason=f"Ticket unclaimed by {member}")
            if saved_pos is not None:
                try:
                    await channel.edit(position=saved_pos)
                except Exception as e:
                    print(f"[Tickets] restore-position failed: {e}")
    except Exception as e:
        print(f"[Tickets] rename/reorder failed: {e}")


# Preferred: a single form (modal) with the Instant/Manual dropdown inside it.
# Dropdowns inside modals require discord.py 2.6+ (discord.ui.Label). Where the
# runtime supports it this is what the user sees; otherwise ticket_close_prompt
# falls back to the plain-dropdown flow below so it can never time out.
class CloseOrderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Close Order", timeout=600)
        self.close_type = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label="Instant Close", value="instant", default=True, description="Close this order right now"),
            discord.SelectOption(label="Manual Close", value="request", description="Ask the opener to confirm first"),
        ])
        # A TextInput wrapped in a Label must NOT carry its own label — the
        # Label provides it. Setting both makes Discord reject the modal
        # (error 50035: "Cannot set label on a TextInput in a Label component").
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False,
            max_length=500, placeholder="Reason for closing (optional)",
        )
        self.add_item(discord.ui.Label(text="Close Type", component=self.close_type))
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))

    async def on_submit(self, interaction):
        mode = self.close_type.values[0] if self.close_type.values else "instant"
        reason = (self.reason.value or "").strip() or "No reason provided."
        if mode == "request":
            await do_request_close(interaction, reason)
        else:
            await do_instant_close(interaction, reason)


# Fallback path: a plain-message dropdown, then a text-only reason box. Used only
# when the single form above isn't supported by the running discord.py build.
class CloseReasonModal(discord.ui.Modal):
    def __init__(self, mode):
        super().__init__(title="Close Order", timeout=600)
        self.mode = mode
        self.reason = discord.ui.TextInput(
            label="Reason", style=discord.TextStyle.paragraph, required=False,
            max_length=500, placeholder="Reason for closing (optional)",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction):
        reason = (self.reason.value or "").strip() or "No reason provided."
        if self.mode == "request":
            await do_request_close(interaction, reason)
        else:
            await do_instant_close(interaction, reason)


def _close_type_view():
    view = discord.ui.View(timeout=300)
    view.add_item(discord.ui.Select(
        custom_id="ticket_closetype", placeholder="Choose how to close…",
        min_values=1, max_values=1, options=[
            discord.SelectOption(label="Instant Close", value="instant", description="Close this order right now"),
            discord.SelectOption(label="Manual Close", value="request", description="Ask the opener to confirm first"),
        ],
    ))
    return view


async def ticket_close_prompt(interaction):
    topic = getattr(interaction.channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        await interaction.response.send_message(embed=error_embed("Not a ticket", "This isn't a ticket channel."), ephemeral=True)
        return
    # Try the single form first. If this runtime can't build a dropdown-in-modal,
    # the modal is never sent (send_modal raises before acking), so we fall back
    # to the plain dropdown instead of leaving the click unanswered.
    if hasattr(discord.ui, "Label"):
        try:
            await interaction.response.send_modal(CloseOrderModal())
            return
        except Exception as e:
            print(f"[Ticket] single-form close modal unavailable ({e}); using dropdown fallback")
    await interaction.response.send_message(
        embed=info_embed("Close Order", "Choose how you'd like to close this order."),
        view=_close_type_view(), ephemeral=True,
    )


async def do_instant_close(interaction, reason):
    channel = interaction.channel
    await interaction.response.send_message(embed=info_embed("Closing order", f"Closed by {interaction.user.mention}\n**Reason:** {reason}\nSaving transcript\u2026"))
    await _do_close(channel, interaction.guild, interaction.user, reason)


async def do_request_close(interaction, reason):
    topic = getattr(interaction.channel, "topic", "") or ""
    opener_id = topic.split("|")[1] if topic.startswith("ticket|") and len(topic.split("|")) > 1 else ""
    mention = f"<@{opener_id}>" if opener_id.isdigit() else ""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Confirm Close", style=discord.ButtonStyle.danger, custom_id="ticket_close_confirm"))
    embed = info_embed("Close requested", f"{interaction.user.mention} requested to close this order.\n**Reason:** {reason}\n\nThe opener or staff can confirm below.")
    await interaction.response.send_message(content=mention or None, embed=embed, view=view)


async def build_transcript(channel):
    lines = [f"Transcript for #{channel.name}", ""]
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            stamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            content = msg.content or ""
            for a in msg.attachments:
                content += f" [attachment: {a.url}]"
            lines.append(f"[{stamp}] {msg.author}: {content}")
    except Exception as e:
        lines.append(f"(transcript error: {e})")
    return "\n".join(lines)


async def record_ticket(guild_id, channel_id, opener_id, category, status):
    await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "ticket_log",
        "_payload": {"guild_id": str(guild_id), "channel_id": str(channel_id), "opener_id": str(opener_id), "category": category, "status": status},
    })


_V2_LAST_ERROR = {"msg": ""}


def _strip_galleries(items):
    """Return a copy of a V2 item tree with all media galleries removed.

    Expired / signed attachment URLs (media.discordapp.net/... ?ex=&is=&hm=)
    are the usual reason Discord rejects a Components V2 message, so dropping
    galleries lets the rest of the design still post."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        t = it.get("type", "")
        if t in ("gallery", "media_gallery", "media"):
            continue
        it = dict(it)
        if isinstance(it.get("children"), list):
            it["children"] = _strip_galleries(it["children"])
        out.append(it)
    return out


async def send_v2_message(channel, components_v2, content=None, interaction=None, ephemeral=False, allowed_mentions=None):
    _guild = getattr(channel, "guild", None)

    def build(comp):
        ctype = comp.get("type", "")
        if ctype in ("text", "text_display"):
            text = comp.get("text") or comp.get("content", "")
            title = comp.get("title", "")
            if title:
                text = f"**{title}**\n{text}" if text else f"**{title}**"
            return {"type": 10, "content": _render_guild_text(text, _guild)} if text else None
        if ctype == "container":
            accent = comp.get("accentColor") or comp.get("accent_color", "")
            try:
                accent_int = int(str(accent).lstrip("#"), 16) if accent else None
            except Exception:
                accent_int = None
            children = [build(c) for c in comp.get("children", [])]
            children = [c for c in children if c]
            if not children:
                return None
            obj = {"type": 17, "components": children}
            if accent_int is not None:
                obj["accent_color"] = accent_int
            return obj
        if ctype == "separator":
            spacing = comp.get("spacing", "small")
            return {"type": 14, "divider": comp.get("divider", True), "spacing": 2 if spacing == "large" else 1}
        if ctype in ("gallery", "media_gallery", "media"):
            urls = comp.get("images") or comp.get("image_urls", [])
            items = [{"media": {"url": u}} for u in urls if u and str(u).startswith("http")]
            return {"type": 12, "items": items} if items else None
        if ctype == "section":
            text = comp.get("text") or comp.get("content", "")
            title = comp.get("title", "")
            if title:
                text = f"**{title}**\n{text}" if text else f"**{title}**"
            if not text:
                return None
            text = _render_guild_text(text, _guild)
            thumb = comp.get("thumbnailUrl") or comp.get("thumbnail_url")
            button = comp.get("button")
            accessory = None
            if thumb and str(thumb).startswith("http"):
                accessory = {"type": 11, "media": {"url": thumb}}
            elif isinstance(button, dict) and button.get("label"):
                accessory = build_button(button, _guild)
            # A Components V2 Section (type 9) REQUIRES an accessory (thumbnail or
            # button). If the design has neither, Discord rejects the whole
            # message, so render the text as a plain text display instead.
            if accessory is None:
                return {"type": 10, "content": text}
            return {"type": 9, "components": [{"type": 10, "content": text}], "accessory": accessory}
        if ctype in ("buttonRow", "button_row", "buttons", "action_row"):
            buttons = [build_button(b, getattr(channel, "guild", None)) for b in comp.get("buttons", [])]
            buttons = [b for b in buttons if b]
            return {"type": 1, "components": buttons} if buttons else None
        if ctype in ("select_menu", "select"):
            placeholder = comp.get("placeholder", "Select an option")
            options = []
            has_category = False
            for opt in comp.get("options", []):
                label = opt.get("label", "Option")
                category = opt.get("category", "")
                channel_id = opt.get("channel_id", "")
                url = opt.get("url", "")
                if "ticket" in opt:
                    has_category = True
                    value = f"ticket_msg:{_comp_key(opt)}"
                elif "form" in opt:
                    has_category = True
                    value = f"ticket_form:{_comp_key(opt)}"
                elif "ephemeral" in opt:
                    has_category = True
                    value = f"eph:{_comp_key(opt)}"
                elif category:
                    has_category = True
                    value = category
                elif channel_id:
                    value = f"ch:{channel_id}"
                elif url:
                    value = f"url:{url}"[:100]
                else:
                    value = label[:100]
                opt_label, opt_emoji = _extract_button_emoji(label)
                o = {"label": (opt_label or label)[:100], "value": value[:100]}
                if opt_emoji:
                    o["emoji"] = opt_emoji
                if opt.get("description"):
                    o["description"] = opt["description"][:100]
                options.append(o)
            if not options:
                return None
            custom_id = "ticket_select" if has_category else f"select_{placeholder[:20]}"
            return {"type": 1, "components": [{"type": 3, "custom_id": custom_id, "placeholder": placeholder[:150], "options": options}]}
        return None

    built = [b for b in (build(c) for c in components_v2) if b]
    if not built:
        return False
    # These component types are all valid at the top level of a Components V2
    # message, so images (12), sections (9), action rows (1), separators (14),
    # etc. can live OUTSIDE a container. Only wrap if something invalid slips in.
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    top_types = {c.get("type") for c in built}
    if not top_types.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    flags = 1 << 15
    if ephemeral:
        flags |= 1 << 6
    payload = {"components": built, "flags": flags}
    if content:
        payload["content"] = content
    # Components V2 messages don't fire mention notifications unless the payload
    # explicitly allows them, so a <@&role> in a ticket message renders but never
    # pings without this.
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    if interaction is not None:
        route = discord.http.Route("POST", "/webhooks/{application_id}/{interaction_token}", application_id=bot.application_id, interaction_token=interaction.token)
    else:
        route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    try:
        resp = await bot.http.request(route, json=payload)
        # Return the new message id (truthy) so callers can track/replace it.
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        return True
    except discord.HTTPException as e:
        body = getattr(e, "text", "") or ""
        _V2_LAST_ERROR["msg"] = f"HTTP {getattr(e, 'status', '?')}: {body[:400]}"
        print(f"[V2] send failed: HTTP {getattr(e, 'status', '?')} {body[:600]}")
        return False
    except Exception as e:
        _V2_LAST_ERROR["msg"] = str(e)[:400]
        print(f"[V2] send failed: {e}")
        return False


_BUTTON_EMOJI_RE = re.compile(r"<(a?):([a-zA-Z0-9_]+):(\d+)>")


def _extract_button_emoji(label):
    match = _BUTTON_EMOJI_RE.search(label)
    if not match:
        return label, None
    emoji = {"id": match.group(3), "name": match.group(2), "animated": bool(match.group(1))}
    clean = (label[: match.start()] + label[match.end():]).strip()
    return clean, emoji


def build_button(btn, guild):
    label = btn.get("label", "Button")
    category = btn.get("category", "")
    channel_id = btn.get("channel_id", "")
    url = btn.get("url", "")
    style_name = str(btn.get("style", "primary")).lower()
    # Resolve :emoji: shortcodes and {count}-style variables in the label so a
    # button labeled ":w_love: {count}" shows the emoji + live count.
    label = _render_guild_text(label, guild)
    label, emoji = _extract_button_emoji(label)

    def _btn(data):
        if emoji:
            data["emoji"] = emoji
        if label:
            data["label"] = label[:80]
        elif "label" in data:
            del data["label"]
        return data

    if btn.get("__verify"):
        return _btn({"type": 2, "label": (label[:80] or "Verify"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": "roblox_verify"})
    if btn.get("__ticket_open"):
        cat = str(btn.get("category") or "support")[:80]
        return _btn({"type": 2, "label": (label[:80] or "Open Ticket"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_cat:{cat}"})
    if btn.get("__ticket_claim"):
        return _btn({"type": 2, "label": (label[:80] or "Claim"), "style": 3, "custom_id": "ticket_claim"})
    if btn.get("__ticket_unclaim"):
        return _btn({"type": 2, "label": (label[:80] or "Unclaim"), "style": 2, "custom_id": "ticket_unclaim"})
    if btn.get("__ticket_close"):
        return _btn({"type": 2, "label": (label[:80] or "Close Order"), "style": BUTTON_STYLE_MAP.get(style_name, 4), "custom_id": "ticket_close"})
    if btn.get("disabled"):
        cid = f"display_{btn.get('id') or label[:20] or 'x'}"
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": cid[:100], "disabled": True})
    if "ticket" in btn:
        key = _comp_key(btn)
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_msg:{key}"})
    if "form" in btn:
        key = _comp_key(btn)
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_form:{key}"})
    if "ephemeral" in btn:
        key = _comp_key(btn)
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"eph:{key}"})
    if category:
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_cat:{category[:80]}"})
    if channel_id:
        gid = getattr(guild, "id", 0)
        return _btn({"type": 2, "label": label[:80], "style": 5, "url": f"https://discord.com/channels/{gid}/{channel_id}"})
    if url:
        return _btn({"type": 2, "label": label[:80], "style": 5, "url": url})
    return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"btn_{label[:20] or 'x'}"})


def build_embed(data, guild=None):
    def _r(v):
        return _render_guild_text(v, guild) if isinstance(v, str) else v
    try:
        color = int(data.get("color")) if data.get("color") is not None else ACCENT
    except Exception:
        color = ACCENT
    embed = discord.Embed(color=color)
    if data.get("title"):
        embed.title = _r(data["title"])
    if data.get("title_url"):
        embed.url = data["title_url"]
    if data.get("description"):
        embed.description = _r(data["description"])
    author = data.get("author")
    if isinstance(author, dict) and author.get("name"):
        embed.set_author(name=_r(author["name"]), icon_url=author.get("icon_url") or None)
    footer = data.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        embed.set_footer(text=_r(footer["text"]), icon_url=footer.get("icon_url") or None)
    for f in data.get("fields", []) or []:
        if f.get("name") and f.get("value"):
            embed.add_field(name=_r(f["name"]), value=_r(f["value"]), inline=bool(f.get("inline")))
    if data.get("thumbnail_url"):
        embed.set_thumbnail(url=data["thumbnail_url"])
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    if data.get("timestamp"):
        embed.timestamp = discord.utils.utcnow()
    return embed


async def handle_post(channel, payload):
    components_v2 = payload.get("components_v2")
    if components_v2:
        await send_v2_message(channel, components_v2, payload.get("content") or None)
        return
    embeds_data = payload.get("embeds") or []
    _guild = getattr(channel, "guild", None)
    content = _render_guild_text(payload.get("content") or "", _guild) or None
    embeds = [build_embed(e, _guild) for e in embeds_data if isinstance(e, dict)]
    extra_images = payload.get("images") or []
    for url in extra_images[1:10]:
        eb = discord.Embed(color=ACCENT)
        eb.set_image(url=url)
        embeds.append(eb)
    if embeds:
        await channel.send(content=content, embeds=embeds[:10])
    elif content:
        await channel.send(content=content)
    for extra in payload.get("trailing_messages", []) or []:
        if extra:
            await channel.send(extra)


async def resolve_channel(channel_id):
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:
            channel = None
    return channel


async def apply_config(feature, cfg, post_panel=False):
    if not isinstance(cfg, dict):
        return
    if feature in ("welcome", "join-logs", "welcome-logs"):
        if "enabled" in cfg:
            welcome_config["enabled"] = bool(cfg["enabled"])
        if cfg.get("channel_id"):
            welcome_config["channel_id"] = str(cfg["channel_id"])
        if cfg.get("message") is not None:
            welcome_config["message"] = cfg.get("message") or ""
        print(f"[Config] welcome — channel {welcome_config['channel_id']} enabled {welcome_config['enabled']}")
    elif feature in ("tickets", "ticket-panels"):
        if cfg.get("category_id"):
            ticket_config["category_id"] = str(cfg["category_id"])
        if cfg.get("support_role_ids") is not None:
            ticket_config["support_role_ids"] = [str(x) for x in cfg["support_role_ids"] if x]
        if cfg.get("log_channel_id"):
            ticket_config["log_channel_id"] = str(cfg["log_channel_id"])
        if cfg.get("open_message") is not None:
            ticket_config["open_message"] = cfg.get("open_message") or ""
        if "ping_support" in cfg:
            ticket_config["ping_support"] = bool(cfg["ping_support"])
        if "one_per_user" in cfg:
            ticket_config["one_per_user"] = bool(cfg["one_per_user"])
        if cfg.get("panel_channel_id"):
            ticket_config["panel_channel_id"] = str(cfg["panel_channel_id"])
        pc = cfg.get("panel_components")
        ticket_config["panel_components"] = pc if isinstance(pc, list) else []
        _register_ticket_components(ticket_config["panel_components"])
        # Ticket types (each with its own button + opening message).
        raw_types = cfg.get("ticket_types")
        if isinstance(raw_types, list) and raw_types:
            types = []
            for t in raw_types:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                types.append({
                    "id": str(t.get("id")),
                    "name": str(t.get("name") or "Ticket"),
                    "button_label": str(t.get("button_label") or "Open Ticket"),
                    "button_style": str(t.get("button_style") or "primary"),
                    "open_components": t.get("open_components") if isinstance(t.get("open_components"), list) else [],
                })
            ticket_config["types"] = types
        else:
            # Legacy single-type fallback (from the earlier single open message).
            oc = cfg.get("open_components")
            ticket_config["types"] = [{
                "id": "support", "name": "Support",
                "button_label": str(cfg.get("open_button_label") or "Open Ticket"),
                "button_style": str(cfg.get("open_button_style") or "primary"),
                "open_components": oc if isinstance(oc, list) else [],
            }]
        print(f"[Config] tickets — category {ticket_config['category_id']} roles {ticket_config['support_role_ids']} panel_ch {ticket_config['panel_channel_id']} panel {len(ticket_config['panel_components'])} types {len(ticket_config['types'])}")
        # Post/refresh the ticket panel on a save/apply (not on boot).
        if post_panel:
            await post_ticket_panel()
    elif feature == "credits":
        if cfg.get("manager_role_ids") is not None:
            credits_config["manager_role_ids"] = [str(x) for x in cfg["manager_role_ids"] if x]
        if cfg.get("currency_name"):
            credits_config["currency_name"] = cfg["currency_name"]
        if cfg.get("log_channel_id"):
            credits_config["log_channel_id"] = str(cfg["log_channel_id"])
        print(f"[Config] credits — managers {credits_config['manager_role_ids']}")
    elif feature == "invite":
        if cfg.get("channel_id"):
            invite_config["channel_id"] = str(cfg["channel_id"])
        comps = cfg.get("components")
        invite_config["components"] = comps if isinstance(comps, list) else []
        embeds = cfg.get("embeds")
        invite_config["embeds"] = embeds if isinstance(embeds, list) else []
        msgs = cfg.get("messages")
        invite_config["messages"] = msgs if isinstance(msgs, list) else []
        print(f"[Config] invite — channel {invite_config['channel_id']} components {len(invite_config['components'])} embeds {len(invite_config['embeds'])}")
    elif feature in ("roblox-verify", "verification"):
        roblox_config["channel_id"] = str(cfg.get("channel_id") or "")
        roblox_config["verified_role_id"] = str(cfg.get("verified_role_id") or "")
        roblox_config["set_nickname"] = bool(cfg.get("set_nickname", True))
        roblox_config["log_channel_id"] = str(cfg.get("log_channel_id") or "")
        roblox_config["client_id"] = str(cfg.get("roblox_client_id") or "")
        roblox_config["client_secret"] = str(cfg.get("roblox_client_secret") or "")
        comps = cfg.get("components")
        roblox_config["components"] = comps if isinstance(comps, list) else []
        roblox_config["button_label"] = str(cfg.get("verify_button_label") or "Verify")
        roblox_config["button_style"] = str(cfg.get("verify_button_style") or "primary")
        print(f"[Config] roblox-verify — channel {roblox_config['channel_id']} role {roblox_config['verified_role_id']} nick {roblox_config['set_nickname']} components {len(roblox_config['components'])}")
        # Post the panel when this came from a save/apply (deliberate action),
        # but NOT on boot — that avoids the surprise repost on every restart.
        # _replace_panel dedupes so a re-post replaces the old panel.
        if post_panel:
            await post_verify_panel()


async def _replace_panel(new_channel_id, new_message_id):
    """Record the freshly-posted panel and delete the previous one, so posting
    again REPLACES the old panel instead of stacking duplicates."""
    old = roblox_config.get("panel_ref")
    roblox_config["panel_ref"] = (
        {"channel_id": str(new_channel_id), "message_id": str(new_message_id)}
        if new_message_id and new_message_id is not True
        else None
    )
    if old and old.get("message_id"):
        try:
            ch = await resolve_channel(old.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(int(old["message_id"]))
                await msg.delete()
        except Exception:
            pass


async def _log_verify(text):
    """Post a diagnostic line to the verify log channel, if one is set."""
    ch = await resolve_channel(roblox_config.get("log_channel_id"))
    if not ch:
        return
    try:
        await ch.send(text[:1900])
    except Exception:
        pass


async def post_verify_panel():
    """(Re)post the Verify panel with the Roblox verify button.

    If the owner designed a custom panel in the dashboard (components), render
    that and attach the Verify button underneath. Otherwise post a default
    embed + button.
    """
    ch = await resolve_channel(roblox_config.get("channel_id"))
    if not ch:
        return

    btn_label = roblox_config.get("button_label") or "Verify"
    btn_style = roblox_config.get("button_style") or "primary"
    verify_row = {"type": "buttonRow", "buttons": [{"label": btn_label, "style": btn_style, "__verify": True}]}
    comps = roblox_config.get("components") or []

    def _with_button(source):
        # Tuck the Verify button inside a container (with the text) so it doesn't
        # dangle at the very bottom outside the box. Prefer the last container;
        # if the design has none, add it as a top-level sibling row.
        panel = [dict(c) for c in source]
        container_idxs = [i for i, c in enumerate(panel) if c.get("type") == "container"]
        if container_idxs:
            i = container_idxs[-1]
            panel[i] = dict(panel[i])
            panel[i]["children"] = list(panel[i].get("children") or []) + [verify_row]
        else:
            panel.append(verify_row)
        return panel

    if comps:
        _V2_LAST_ERROR["msg"] = ""
        # Attempt 1: the panel exactly as designed in the dashboard.
        try:
            mid = await send_v2_message(ch, _with_button(comps))
            if mid:
                print("[Verify] custom panel posted")
                await _replace_panel(ch.id, mid)
                return
        except Exception as e:
            print(f"[Verify] custom panel error: {e}")

        # Attempt 2: retry with media galleries removed — a rejected image URL
        # is the most common reason a Components V2 message fails to send.
        stripped = _strip_galleries(comps)
        if stripped != comps:
            try:
                mid = await send_v2_message(ch, _with_button(stripped))
                if mid:
                    print("[Verify] custom panel posted (images dropped — an image URL was rejected)")
                    await _replace_panel(ch.id, mid)
                    await _log_verify(f"⚠️ Verify panel posted without its image(s): Discord rejected the image URL. {_V2_LAST_ERROR['msg']}")
                    return
            except Exception as e:
                print(f"[Verify] stripped panel error: {e}")

        # Both attempts failed — surface the real reason instead of silently
        # posting an unrelated default that looks like 'a random thing'.
        print(f"[Verify] custom panel failed twice, using default. reason={_V2_LAST_ERROR['msg']}")
        await _log_verify(f"⚠️ Your custom Verify panel could not be posted, so the default was used. Reason: {_V2_LAST_ERROR['msg'] or 'unknown'}")
    else:
        # No components saved at all — tell the owner so they know the default
        # is showing because nothing was designed/saved, not because it broke.
        print("[Verify] no custom components saved — posting default panel")
        await _log_verify("ℹ️ No custom Verify panel was saved, so the default is being used. Design one in the dashboard and press Save changes.")

    embed = discord.Embed(
        title="Verify with Roblox",
        description="Click **Verify** to link your Roblox account. Once you're done, your nickname is set to your Roblox name and you get access to the server.",
        color=0x2B2D31,
    )
    _style_map = {
        "primary": discord.ButtonStyle.primary,
        "success": discord.ButtonStyle.success,
        "secondary": discord.ButtonStyle.secondary,
        "danger": discord.ButtonStyle.danger,
    }
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=(btn_label or "Verify")[:80],
        style=_style_map.get(btn_style, discord.ButtonStyle.primary),
        custom_id="roblox_verify",
    ))
    try:
        msg = await ch.send(embed=embed, view=view)
        await _replace_panel(ch.id, msg.id)
    except Exception as e:
        print(f"[Verify] panel post failed: {e}")


async def _replace_ticket_panel(new_channel_id, new_message_id):
    """Record the freshly-posted ticket panel and delete the previous one, so a
    re-post replaces it instead of stacking duplicates."""
    old = ticket_config.get("panel_ref")
    ticket_config["panel_ref"] = (
        {"channel_id": str(new_channel_id), "message_id": str(new_message_id)}
        if new_message_id and new_message_id is not True else None
    )
    if old and old.get("message_id"):
        try:
            ch = await resolve_channel(old.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(int(old["message_id"]))
                await msg.delete()
        except Exception:
            pass


async def post_ticket_panel():
    """(Re)post the ticket panel — the owner's designed message plus an Open
    Ticket button — to the configured panel channel. Mirrors the verify panel."""
    ch = await resolve_channel(ticket_config.get("panel_channel_id"))
    if not ch:
        return
    types = ticket_config.get("types") or []
    if not types:
        types = [{"id": "support", "name": "Support", "button_label": "Open Ticket", "button_style": "primary"}]
    # One Open button per ticket type, chunked into rows of 5 (Discord's limit).
    open_rows = []
    current = []
    for t in types:
        current.append({
            "label": t.get("button_label") or "Open Ticket",
            "style": t.get("button_style") or "primary",
            "__ticket_open": True,
            "category": t.get("id") or "support",
        })
        if len(current) == 5:
            open_rows.append({"type": "buttonRow", "buttons": current})
            current = []
    if current:
        open_rows.append({"type": "buttonRow", "buttons": current})
    comps = ticket_config.get("panel_components") or []

    def _with_button(source):
        panel = [dict(c) for c in source]
        container_idxs = [i for i, c in enumerate(panel) if c.get("type") == "container"]
        if container_idxs:
            i = container_idxs[-1]
            panel[i] = dict(panel[i])
            panel[i]["children"] = list(panel[i].get("children") or []) + open_rows
        else:
            panel.extend(open_rows)
        return panel

    if comps:
        try:
            mid = await send_v2_message(ch, comps)
            if mid:
                print("[Tickets] panel posted")
                await _replace_ticket_panel(ch.id, mid)
                return
        except Exception as e:
            print(f"[Tickets] panel error: {e}")
        stripped = _strip_galleries(comps)
        if stripped != comps:
            try:
                mid = await send_v2_message(ch, stripped)
                if mid:
                    print("[Tickets] panel posted (images dropped)")
                    await _replace_ticket_panel(ch.id, mid)
                    return
            except Exception as e:
                print(f"[Tickets] stripped panel error: {e}")

    # Default panel (no custom design, or the custom one wouldn't send) — a
    # button per type on a classic embed.
    embed = discord.Embed(
        title="Support Tickets",
        description="Need help? Pick an option below and our team will be with you.",
        color=ACCENT,
    )
    _style_map = {
        "primary": discord.ButtonStyle.primary, "success": discord.ButtonStyle.success,
        "secondary": discord.ButtonStyle.secondary, "danger": discord.ButtonStyle.danger,
    }
    view = discord.ui.View(timeout=None)
    for t in types[:25]:
        view.add_item(discord.ui.Button(
            label=(t.get("button_label") or "Open Ticket")[:80],
            style=_style_map.get(t.get("button_style") or "primary", discord.ButtonStyle.primary),
            custom_id=f"ticket_cat:{(t.get('id') or 'support')[:80]}",
        ))
    try:
        msg = await ch.send(embed=embed, view=view)
        await _replace_ticket_panel(ch.id, msg.id)
    except Exception as e:
        print(f"[Tickets] panel post failed: {e}")


async def apply_roblox_verification(payload):
    """Bot side of a completed Roblox verify: set nickname + give the role."""
    guild = bot.get_guild(int(payload["guild_id"])) if payload.get("guild_id") else None
    if not guild:
        return
    uid = payload.get("discord_user_id")
    member = guild.get_member(int(uid)) if uid else None
    if member is None and uid:
        try:
            member = await guild.fetch_member(int(uid))
        except Exception:
            member = None
    if not member:
        return
    roblox_username = (payload.get("roblox_username") or "").strip()
    notes = []

    # Nickname
    if roblox_config.get("set_nickname", True) and roblox_username:
        try:
            await member.edit(nick=roblox_username[:32], reason="Roblox verified")
        except discord.Forbidden:
            notes.append("• Couldn't set nickname — I need **Manage Nicknames**, and I can't rename the server owner or anyone with a role above mine.")
            print("[Verify] nickname change forbidden")
        except Exception as e:
            notes.append(f"• Couldn't set nickname — {e}")
            print(f"[Verify] nickname change failed: {e}")

    # Verified role
    role_id = str(roblox_config.get("verified_role_id") or "").strip()
    if not role_id:
        notes.append("• No Verified role is set in the dashboard — open the Verification block, pick a role, and Save.")
        print("[Verify] no verified_role_id configured")
    else:
        role = guild.get_role(int(role_id))
        if not role:
            notes.append("• The Verified role no longer exists in this server — pick a new one in the dashboard.")
            print(f"[Verify] role {role_id} not found in guild")
        else:
            try:
                await member.add_roles(role, reason="Roblox verified")
                print(f"[Verify] gave {member} the {role.name} role")
            except discord.Forbidden:
                notes.append(
                    f"• Couldn't give the **{role.name}** role — my role must sit **above** it in Server Settings → Roles, "
                    "and I need the **Manage Roles** permission."
                )
                print(f"[Verify] role assign forbidden (hierarchy/perms) for {role.name}")
            except Exception as e:
                notes.append(f"• Couldn't give the **{role.name}** role — {e}")
                print(f"[Verify] role assign failed: {e}")

    # Report the outcome to the log channel so the owner can see it in Discord.
    log_id = str(roblox_config.get("log_channel_id") or "").strip()
    if log_id:
        log_ch = guild.get_channel(int(log_id))
        if log_ch:
            try:
                if notes:
                    await log_ch.send(embed=error_embed(
                        "Verified — but something needs fixing",
                        f"{member.mention} linked **{roblox_username}**, however:\n" + "\n".join(notes),
                    ))
                else:
                    await log_ch.send(embed=success_embed(
                        "Roblox verified",
                        f"{member.mention} linked **{roblox_username}** — nickname and role applied.",
                    ))
            except Exception:
                pass


async def start_roblox_verify(interaction):
    """A member clicked Verify — ask the edge function for their Roblox login URL."""
    await interaction.response.defer(ephemeral=True)
    if not roblox_config.get("client_id"):
        await interaction.followup.send(
            embed=error_embed("Verification not set up", "An admin still needs to add the Roblox Client ID/Secret in the dashboard."),
            ephemeral=True,
        )
        return
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/roblox-verify",
            headers=_fn_headers(),
            json={
                "action": "start",
                "bot_id": BOT_ORDER_ID,
                "guild_id": str(interaction.guild_id),
                "discord_user_id": str(interaction.user.id),
            },
        ) as r:
            data = await r.json() if r.status == 200 else {}
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            await interaction.followup.send(
                embed=error_embed("Couldn't start verification", "Please try again in a moment."),
                ephemeral=True,
            )
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Link Roblox", url=url, style=discord.ButtonStyle.link, emoji="🔗"))
        await interaction.followup.send(
            "Click **Link Roblox** to log in. When Roblox says you're verified, come back here — your nickname and role update automatically.",
            view=view,
            ephemeral=True,
        )
    except Exception as e:
        print(f"[Verify] start failed: {e}")
        await interaction.followup.send(embed=error_embed("Something went wrong", "Please try again."), ephemeral=True)


async def fetch_config(feature):
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return None
    try:
        session = await get_poll_session()
        async with session.get(
            f"{SUPABASE_FN_URL}/{BOT_API}/bot-config?feature={feature}&bot_id={BOT_ORDER_ID}",
            headers=_fn_headers(),
        ) as r:
            if r.status == 200:
                data = await r.json()
                cfg = data.get("config") if isinstance(data, dict) else None
                if isinstance(cfg, dict) and "config" in cfg:
                    cfg = cfg["config"]
                return cfg
            print(f"[Config] fetch {feature} — HTTP {r.status}")
    except Exception as e:
        print(f"[Config] fetch {feature} failed: {e}")
    return None


async def mark_config_applied(feature):
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/mark-config-applied",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "feature": feature},
        )
    except Exception as e:
        print(f"[Config] mark applied {feature} failed: {e}")


async def load_all_configs():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        print(f"[Config] load skipped — BOT_ORDER_ID set: {bool(BOT_ORDER_ID)}, WORKER_TOKEN set: {bool(WORKER_TOKEN)}")
        return
    print(f"[Config] loading for bot {BOT_ORDER_ID}")
    for feature in ("welcome", "invite", "tickets", "credits", "roblox-verify"):
        cfg = await fetch_config(feature)
        if cfg:
            await apply_config(feature, cfg)
        else:
            print(f"[Config] {feature} — none saved")


async def complete_command(command_id, status="done", error=None):
    body = {"command_id": command_id, "status": status}
    if error:
        body["error_message"] = error
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/complete-command", headers=_fn_headers(), json=body)
    except Exception as e:
        print(f"[Command] complete failed: {e}")


_processing_roblox = set()


@tasks.loop(seconds=8)
async def poll_roblox_apply():
    """Claim pending roblox_apply commands straight from the DB via REST and
    process them (nickname + role). This bypasses the shared claim-command
    allowlist, so verification works regardless of the bot-api function's
    action whitelist."""
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/bot_commands?bot_id=eq.{BOT_ORDER_ID}"
            f"&action=eq.roblox_apply&status=eq.pending&order=created_at.asc&select=id,payload&limit=10"
        )
        async with httpx.AsyncClient() as client:
            r = await client.get(
                url,
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=10,
            )
        if r.status_code != 200:
            return
        rows = r.json()
    except Exception as e:
        print(f"[Verify] roblox_apply poll failed: {e}")
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        cid = row.get("id")
        if not cid or cid in _processing_roblox:
            continue
        _processing_roblox.add(cid)
        try:
            print(f"[Verify] processing roblox_apply {cid}")
            await apply_roblox_verification(row.get("payload") or {})
        except Exception as e:
            print(f"[Verify] roblox_apply {cid} failed: {e}")
        finally:
            # Mark done either way so we don't loop on a bad row forever.
            await complete_command(cid)
            _processing_roblox.discard(cid)


@poll_roblox_apply.before_loop
async def before_poll_roblox_apply():
    await bot.wait_until_ready()


async def save_ticket_panel(guild_id, channel_id, message_id, channel_name):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_FN_URL}/save-ticket-panel",
                headers=_fn_headers(),
                json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channel_id": str(channel_id), "message_id": str(message_id), "channel_name": channel_name},
                timeout=10,
            )
    except Exception as e:
        print(f"[Ticket] save panel failed: {e}")


@tasks.loop(seconds=5)
async def poll_configs():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/claim-command",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID},
        ) as r:
            if r.status != 200:
                if r.status not in (401, 403):
                    return
                global _auth_warned
                if not _auth_warned:
                    _auth_warned = True
                    body = (await r.text())[:200]
                    print(f"[Poll] claim-command auth failed — HTTP {r.status} body={body}")
                return
            data = await r.json()
        cmd = data.get("command") if isinstance(data, dict) else None
        if not cmd:
            return
        action = cmd.get("action")
        payload = cmd.get("payload") or {}
        command_id = cmd.get("id")
        print(f"[Poll] {action} ({command_id})")

        if action in ("post_message", "send_channel_message"):
            if payload.get("verify_panel"):
                # Owner pressed "Post panel" for Roblox verification.
                if payload.get("channel_id"):
                    roblox_config["channel_id"] = str(payload["channel_id"])
                await post_verify_panel()
            else:
                channel = await resolve_channel(payload.get("channel_id"))
                if channel:
                    await handle_post(channel, payload)
            await complete_command(command_id)

        elif action == "edit_ticket_panel":
            channel = await resolve_channel(payload.get("channel_id"))
            if channel and payload.get("components_v2"):
                ok = await send_v2_message(channel, payload["components_v2"], payload.get("content") or None)
            await complete_command(command_id)

        elif action == "apply_config":
            feature = payload.get("feature")
            if feature:
                cfg = await fetch_config(feature)
                if cfg:
                    # A save/apply command is a deliberate action, so post the
                    # verify panel here (boot loads config without posting).
                    await apply_config(feature, cfg, post_panel=True)
                await mark_config_applied(feature)
            await complete_command(command_id)

        elif action == "roblox_apply":
            await apply_roblox_verification(payload)
            await complete_command(command_id)

        elif action == "set_status":
            await refresh_status()
            await complete_command(command_id)

        elif action == "list_roles":
            await cache_roles(payload.get("guild_id"))
            await complete_command(command_id)

        elif action == "list_channels":
            await cache_channels(payload.get("guild_id"))
            await complete_command(command_id)

        else:
            await complete_command(command_id)

    except Exception as e:
        print(f"[Poll] error: {e}")


@poll_configs.before_loop
async def before_poll_configs():
    await bot.wait_until_ready()


async def cache_roles(guild_id):
    if not guild_id:
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    now = discord.utils.utcnow().isoformat()
    roles = [{
        "bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "role_id": str(r.id), "role_name": r.name,
        "color": r.color.value, "position": r.position, "managed": r.managed, "is_everyone": r.id == guild.id, "fetched_at": now,
    } for r in guild.roles]
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/upsert-role-cache", headers=_fn_headers(), json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "roles": roles})
    except Exception as e:
        print(f"[Cache] roles failed: {e}")


async def cache_channels(guild_id):
    if not guild_id:
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    now = discord.utils.utcnow().isoformat()
    channels = []
    for ch in guild.channels:
        if isinstance(ch, discord.TextChannel):
            ctype = "text"
        elif isinstance(ch, discord.VoiceChannel):
            ctype = "voice"
        elif isinstance(ch, discord.CategoryChannel):
            ctype = "category"
        else:
            ctype = "other"
        channels.append({
            "bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channel_id": str(ch.id), "channel_name": ch.name,
            "channel_type": ctype, "parent_id": str(ch.category_id) if ch.category_id else None, "position": ch.position, "fetched_at": now,
        })
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/upsert-channel-cache", headers=_fn_headers(), json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channels": channels})
    except Exception as e:
        print(f"[Cache] channels failed: {e}")


async def fire_online_status():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return
    try:
        guilds = [{"id": str(g.id), "name": g.name, "member_count": g.member_count or 0} for g in bot.guilds]
        payload = {"bot_id": BOT_ORDER_ID, "last_heartbeat_at": discord.utils.utcnow().isoformat(), "status": "online"}
        if guilds:
            payload["guilds"] = guilds
        async with httpx.AsyncClient() as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/bot_runtime_status?bot_id=eq.{BOT_ORDER_ID}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=payload, timeout=5,
            )
        print("[Boot] online status fired")
    except Exception as e:
        print(f"[Boot] online status failed: {e}")


@tasks.loop(seconds=30)
async def send_heartbeat():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        guilds = [{"id": str(g.id), "name": g.name, "member_count": g.member_count or 0} for g in bot.guilds]
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/heartbeat",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "status": "online", "guilds": guilds},
        )
    except Exception as e:
        print(f"[Heartbeat] error: {e}")


@send_heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def record_metrics_loop():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/record-metrics",
            headers=_fn_headers(),
            json={
                "bot_id": BOT_ORDER_ID, "commands": 0, "messages": 0, "errors": 0,
                "active_servers": len(bot.guilds), "member_count": sum(g.member_count or 0 for g in bot.guilds),
            },
        )
    except Exception as e:
        print(f"[Metrics] error: {e}")


@record_metrics_loop.before_loop
async def before_metrics():
    await bot.wait_until_ready()


async def apply_bot_identity():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/bot_orders?id=eq.{BOT_ORDER_ID}&select=bot_name",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10,
            )
            data = r.json()
            if not data or not isinstance(data, list) or not data[0].get("bot_name"):
                return
            target = data[0]["bot_name"]
    except Exception as e:
        print(f"[Identity] fetch failed: {e}")
        return
    if bot.user and bot.user.name == target:
        return
    try:
        await bot.user.edit(username=target)
        print(f"[Identity] username set to {target}")
    except discord.HTTPException as e:
        print(f"[Identity] failed: {getattr(e, 'status', '')}")
    except Exception as e:
        print(f"[Identity] error: {e}")


_last_bio = None
_about_me_diag = False


async def apply_about_me():
    """Push the dashboard's About Me to Discord as the application description
    via PATCH /applications/@me (authorised with the bot's own token). Discord
    supports this now, so there's no manual portal step. Only re-sends when the
    text actually changes."""
    global _last_bio, _about_me_diag
    if not (SUPABASE_URL and BOT_ORDER_ID and TOKEN):
        if not _about_me_diag:
            print(f"[AboutMe] skipped — SUPABASE_URL:{bool(SUPABASE_URL)} BOT_ORDER_ID:{bool(BOT_ORDER_ID)} TOKEN:{bool(TOKEN)}")
            _about_me_diag = True
        return
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/bot_orders?id=eq.{BOT_ORDER_ID}&select=bot_bio",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10,
            )
            status = r.status_code
            data = r.json()
        if not isinstance(data, list) or not data:
            if not _about_me_diag:
                print(f"[AboutMe] fetch returned no row — HTTP {status} body={str(data)[:200]}")
                _about_me_diag = True
            return
        bio = data[0].get("bot_bio")
        # One-time diagnostic so we can see exactly what the bot reads.
        if not _about_me_diag:
            print(f"[AboutMe] fetch OK — HTTP {status}, bot_bio={'<empty>' if not bio else repr(bio[:60])}")
            _about_me_diag = True
    except Exception as e:
        print(f"[AboutMe] fetch failed: {e}")
        return
    if bio is None or bio == "" or bio == _last_bio:
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                "https://discord.com/api/v10/applications/@me",
                headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"},
                json={"description": str(bio or "")[:400]},
                timeout=10,
            )
        if resp.status_code in (200, 201):
            # Only mark as applied AFTER Discord accepts it, so a failed attempt
            # retries on the next loop instead of being marked done.
            _last_bio = bio
            print("[AboutMe] application description updated")
        else:
            print(f"[AboutMe] update failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[AboutMe] update error: {e}")


@tasks.loop(hours=2)
async def sync_identity():
    await apply_bot_identity()
    await apply_about_me()


@sync_identity.before_loop
async def before_sync_identity():
    await bot.wait_until_ready()


async def _shutdown():
    print("[Shutdown] shutting down")
    if SUPABASE_URL and BOT_ORDER_ID:
        try:
            async with httpx.AsyncClient() as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/bot_runtime_status?bot_id=eq.{BOT_ORDER_ID}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"status": "offline"}, timeout=5,
                )
        except Exception:
            pass
    try:
        await bot.change_presence(status=discord.Status.invisible)
    except Exception:
        pass
    for loop in (send_heartbeat, poll_configs, record_metrics_loop, poll_roblox_apply, poll_about_me):
        try:
            loop.cancel()
        except Exception:
            pass
    await bot.close()


def handle_sigterm(sig, frame):
    print(f"[Shutdown] signal {sig}")
    asyncio.create_task(_shutdown())


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


async def claim_shutdown_command():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return None
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/bot_commands?bot_id=eq.{BOT_ORDER_ID}&action=eq.shutdown"
            f"&status=eq.pending&created_at=gte.{BOT_START_TIME}&order=created_at.desc&select=id&limit=1"
        )
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5)
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        print(f"[Shutdown] claim error: {e!r}")
    return None


@tasks.loop(seconds=3)
async def poll_shutdown():
    cmd = await claim_shutdown_command()
    if cmd:
        print("[Shutdown] command received")
        await _shutdown()


@poll_shutdown.before_loop
async def before_poll_shutdown():
    await bot.wait_until_ready()


def _run():
    try:
        bot.run(TOKEN)
    except discord.errors.HTTPException as e:
        if getattr(e, "status", None) == 429:
            import time
            import sys
            print("[Boot] rate-limit ban — sleeping 15 minutes")
            time.sleep(900)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        raise


if __name__ == "__main__":
    _run()
