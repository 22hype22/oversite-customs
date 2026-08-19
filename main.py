import os
import io
import re
import json
import hashlib
import signal
import asyncio
import datetime
import time
import random
import secrets

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
    "panels": [],  # [{channel_id, components}, ...] — every panel, all registered/posted
    "types": [],
    "panel_refs": {},  # channel_id -> last panel message id (one panel kept per channel)
}

# Registry mapping a clicked Ticket/Ephemeral component back to the message the
# dashboard designed for it. Rebuilt from panel_components on every apply_config
# (and on boot), so it survives restarts.
ticket_msgs = {}   # key -> open_components (Ticket buttons/options)
eph_msgs = {}      # key -> open_components (Ephemeral buttons/options)
form_msgs = {}     # key -> open_components (Form buttons/options — collect {Question:} answers first)
form_titles = {}   # key -> modal title (the button/option label)
ticket_categories = {}  # key -> category name a Ticket/Form drops its channels into
ticket_access = {}      # key -> comma-separated role names that can see a Ticket/Form's channels

# ---- Giveaways ----
# Look designed in the dashboard "Giveaway" block (feature "customs-giveaway").
# Every field is optional — the bot has sensible defaults so /giveaway works with
# no config at all.
giveaway_config = {
    "title": "🎉 GIVEAWAY 🎉",
    "color": ACCENT,
    "button_label": "🎉 Enter",
    "host_line": "",          # extra line under the prize (rules, host note, etc.)
    "ping": "",               # optional role/text pinged with the giveaway post
    "default_winners": 1,
    "default_duration": "1d",
    "manager_role_ids": [],   # roles (besides Manage Server) allowed to run /giveaway
    "components": [],         # optional V2 design shown while the giveaway runs
    "ended_components": [],   # optional V2 design shown once the giveaway ends
}
# Live giveaways this process is tracking. Keyed by a short giveaway id (gid) that
# also lives in the Enter button's custom_id, so entries route back here.
# gid -> {message_id, channel_id, guild_id, prize, winners, end_ts, host_id,
#         entrants:set[str], ended:bool}
active_giveaways = {}

# ---- Robux Locker ----
# Panel designed in the dashboard "Robux Locker" block. Members buy Robux from it.
# `stock` = Available Stock (Robux staff allocated via /robuxlocker); shown on the
# panel via the {stock} token and decremented as members buy.
robux_locker_config = {"channel_id": "", "components": [], "panel_ref": None, "stock": 0, "last_funds": 0, "rate_per_1k": 0.0}

# ---- Portfolio ----
# Post designed in the dashboard "Portfolio" block. Running /portfolio sends the
# design to the configured channel.
portfolio_config = {"channel_id": "", "components": [], "allowed_role_ids": []}

# ---- Order Status ----
# Configured in the dashboard "Order Status" block. An "Order Status" button
# shows a live embed: each service is Open / Oversite+ only / Closed based on how
# many order tickets are open in that service's category.
order_status_config = {
    "title": "Order Status",
    "limited_at": 8, "closed_at": 10,
    "emoji_open": "", "label_open": "Open",
    "emoji_limited": "", "label_limited": "Oversite+ Only",
    "emoji_closed": "", "label_closed": "Closed",
    "services": [],  # list of {"name", "category"}
}

# ---- Pricing ----
# Structure (services + their items) comes from the dashboard "Pricing" block.
# Designers fill in the actual prices from Discord via /setpricing; members view
# them via /pricing. Prices persist server-side (pricing edge function).
pricing_config = {
    "designer_role_ids": [],
    "currency": "$",
    "title": "Pricing",
    "services": [],     # list of {"name": str, "items": [str, ...]}
    "values": {},       # { service_name: { item_name: {robux, usd} } }
    "components": [],   # dashboard-designed /pricing layout ({service}, {pricing} tokens)
}


def _parse_pricing_services(raw):
    """Parse 'Service: item1, item2, item3' lines into [{name, items:[...]}]."""
    out = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, items_str = line.split(":", 1)
            name = name.strip()
            items = [i.strip() for i in items_str.split(",") if i.strip()]
        else:
            name, items = line, []
        if name:
            out.append({"name": name, "items": items})
    return out


def _parse_order_services(raw):
    """Parse the dashboard textarea (one 'Name = Category' per line) into a list
    of {name, category}. A line with no '=' uses the name as the category too."""
    out = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            name, cat = line.split("=", 1)
            name, cat = name.strip(), cat.strip()
        else:
            name, cat = line, line
        if name:
            out.append({"name": name, "category": cat or name})
    return out


def _order_slug(name):
    """Token slug from a service's display name: letters + digits, lowercased.
    'Liveries' -> 'liveries', 'Bot Design' -> 'botdesign', 'GFX' -> 'gfx'."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _order_status_for(guild, svc):
    """(emoji, label) for one service based on its current open ticket count."""
    limited_at = int(order_status_config.get("limited_at") or 8)
    closed_at = int(order_status_config.get("closed_at") or 10)
    cat = svc.get("category") or svc.get("name") or ""
    count = _open_ticket_count_for_category(guild, cat)
    if count >= closed_at:
        return (order_status_config.get("emoji_closed") or "", order_status_config.get("label_closed") or "Closed")
    if count >= limited_at:
        return (order_status_config.get("emoji_limited") or "", order_status_config.get("label_limited") or "Oversite+ Only")
    return (order_status_config.get("emoji_open") or "", order_status_config.get("label_open") or "Open")


_ORDER_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


def _render_order_tokens(text, guild):
    """Replace per-service status tokens anywhere in text. For a service named
    'Liveries':
      {liveries}       -> just the status ICON (e.g. 🟢)
      {liveriesstatus} -> icon + word (e.g. '🟢 Open')
    Matching is case- and space-insensitive, so {Liveries}, { Clothing } and
    {GFX} all resolve. Emojis stay as :shortcodes: — _render_guild_text resolves
    them after."""
    if not (guild and isinstance(text, str)) or "{" not in text:
        return text
    by_slug = {}
    for svc in (order_status_config.get("services") or []):
        s = _order_slug(svc.get("name"))
        if s:
            by_slug[s] = svc
    if not by_slug:
        return text

    def _sub(m):
        key = _order_slug(m.group(1))  # lowercase, letters+digits only
        status_only = False
        svc = by_slug.get(key)
        if svc is None and key.endswith("status"):
            svc = by_slug.get(key[:-6])
            status_only = True
        if svc is None:
            return m.group(0)  # not one of ours — leave it exactly as typed
        emoji, lbl = _order_status_for(guild, svc)
        emoji = (emoji or "").strip()
        if status_only:
            return f"{emoji} {lbl}".strip()
        # Default token = 'Name — <emoji>' (no Open/Closed word). Falls back to
        # the word only if no emoji is configured.
        name = svc.get("name") or ""
        return f"{name} — {emoji}" if emoji else f"{name} — {lbl}"

    return _ORDER_TOKEN_RE.sub(_sub, text)


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

def _register_ticket_components(panels):
    """Register the interactive components (Ticket/Form/Ephemeral) from EVERY
    panel so all posted panels keep working — not just the most recent one.
    `panels` is a list of component-trees (one per panel). A single tree is also
    accepted for backward compatibility."""
    ticket_msgs.clear(); eph_msgs.clear(); form_msgs.clear(); form_titles.clear(); ticket_categories.clear(); ticket_access.clear()

    def _reg(x):
        oc = x.get("open_components") or []
        if "ticket" in x:
            k = _comp_key(x)
            ticket_msgs[k] = oc
            ticket_categories[k] = (x.get("category_name") or "").strip()
            ticket_access[k] = (x.get("access_roles") or "").strip()
        elif "form" in x:
            k = _comp_key(x)
            form_msgs[k] = oc
            form_titles[k] = x.get("label") or "Application"
            ticket_categories[k] = (x.get("category_name") or "").strip()
            ticket_access[k] = (x.get("access_roles") or "").strip()
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

    # Accept a single tree (list of items) or a list of trees (one per panel).
    trees = panels or []
    if trees and isinstance(trees[0], dict):
        trees = [trees]
    for tree in trees:
        if isinstance(tree, list):
            walk(tree, 0)
    print(f"[Tickets] registry: {len(ticket_msgs)} ticket + {len(form_msgs)} form + {len(eph_msgs)} ephemeral messages")
    print(f"[Tickets] registry built: tickets={{{', '.join(f'{k}:{len(v)}' for k,v in ticket_msgs.items())}}} eph={{{', '.join(f'{k}:{len(v)}' for k,v in eph_msgs.items())}}}")


# Ticket panels can come from more than one dashboard block — the main "Tickets"
# block and the "Order Log" block. Each registers its panels + types here under
# its feature key; the registry, posted-panel list, and type list are rebuilt
# from ALL sources so neither block wipes the other's buttons. Order-log tickets
# route by each Ticket/Form button's own category + access roles, so they open in
# their own category independently of regular tickets.
_ticket_sources = {}  # feature -> {"panels": [{channel_id, components}], "types": [type defs]}


def _parse_ticket_panels(cfg):
    raw_panels = cfg.get("panels")
    panels = []
    if isinstance(raw_panels, list) and raw_panels:
        for p in raw_panels:
            if not isinstance(p, dict):
                continue
            comps = p.get("components")
            panels.append({
                "channel_id": str(p.get("channel_id") or ""),
                "components": comps if isinstance(comps, list) else [],
            })
    if not panels:
        pc = cfg.get("panel_components")
        panels.append({
            "channel_id": str(cfg.get("panel_channel_id") or ""),
            "components": pc if isinstance(pc, list) else [],
        })
    return panels


def _parse_ticket_types(cfg):
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
        return types
    oc = cfg.get("open_components")
    return [{
        "id": "support", "name": "Support",
        "button_label": str(cfg.get("open_button_label") or "Open Ticket"),
        "button_style": str(cfg.get("open_button_style") or "primary"),
        "open_components": oc if isinstance(oc, list) else [],
    }]


def _rebuild_ticket_registry():
    """Rebuild the interactive-component registry AND the union panel/type lists
    from every registered ticket source (main Tickets + Order Log)."""
    trees = []
    for src in _ticket_sources.values():
        for p in src.get("panels", []):
            comps = p.get("components")
            if isinstance(comps, list):
                trees.append(comps)
    _register_ticket_components(trees)
    ticket_config["panels"] = [p for src in _ticket_sources.values() for p in src.get("panels", [])]
    ticket_config["types"] = [t for src in _ticket_sources.values() for t in src.get("types", [])]


# Who may OPEN an order-log ticket. `keys` is the set of registry keys that
# belong to the Order Log panel (its Ticket/Form/Ephemeral buttons); `role_ids`
# gates them. Regular ticket keys aren't in `keys`, so they're never gated here.
orderlog_gate = {"role_ids": [], "keys": set()}


def _collect_ticket_keys(trees):
    """Set of registry keys (_comp_key) for the Ticket/Form/Ephemeral components
    in the given panel trees — used to know which buttons are order-log ones."""
    keys = set()

    def _reg(x):
        if ("ticket" in x) or ("form" in x) or ("ephemeral" in x):
            keys.add(_comp_key(x))
        oc = x.get("open_components") or []
        if oc:
            walk(oc)

    def walk(items):
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "container":
                walk(c.get("children") or c.get("components") or [])
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

    for tree in trees or []:
        if isinstance(tree, list):
            walk(tree)
    return keys


def _orderlog_can_open(member):
    """Allowed if no roles are set (open to all), or member has an allowed role,
    or has Manage Server."""
    if not orderlog_gate["role_ids"]:
        return True
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, orderlog_gate["role_ids"])


async def _orderlog_gate_block(interaction, key):
    """If `key` is an order-log button and the user isn't allowed, deny + return
    True (caller should stop). Otherwise return False."""
    if key in orderlog_gate["keys"] and not _orderlog_can_open(interaction.user):
        try:
            await interaction.response.send_message(
                embed=error_embed("No permission", "You don't have a role allowed to open an order log."),
                ephemeral=True,
            )
        except Exception:
            pass
        return True
    return False
credits_config = {"manager_role_ids": CREDIT_MANAGER_ROLE_IDS, "currency_name": "credits", "log_channel_id": ""}
_credits_memory = {}
# Roblox OAuth verification config (from the dashboard "Verification" block).
roblox_config = {
    "channel_id": "",
    "verified_role_ids": [],
    "remove_role_ids": [],
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

    # Restore every saved giveaway (entrants + timers) so redeploys never drop them.
    try:
        await _gw_restore_all()
    except Exception as e:
        print(f"[Startup] giveaway restore failed: {e}")

    if not update_status.is_running():
        update_status.start()
    if not portfolio_cleanup.is_running():
        portfolio_cleanup.start()
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
    if ":" not in text:
        return text
    lookup = {e.name.lower(): e for e in (guild.emojis if guild else [])}
    # Bots can use custom emojis from ANY server they're in, so fall back to the
    # bot's full emoji set (e.g. a shared emoji server) for anything not found in
    # this guild. This is why an :emoji: from another server still renders.
    try:
        for e in bot.emojis:
            lookup.setdefault(e.name.lower(), e)
    except Exception:
        pass
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


def _resolve_role_mentions(text, guild):
    """Turn a plain '@Role Name' typed in the dashboard into a real <@&id> role
    mention so it actually pings. Matches against the guild's real role names,
    longest first (so 'Livery Designer' wins over 'Livery')."""
    if not text or "@" not in text or not guild:
        return text
    for role in sorted(guild.roles, key=lambda r: len(r.name), reverse=True):
        if role.is_default() or not role.name:
            continue
        text = re.sub(r"@" + re.escape(role.name), f"<@&{role.id}>", text, flags=re.IGNORECASE)
    return text


_CHANNEL_TOKEN_RE = re.compile(r"#([a-zA-Z0-9_\-]+)")


def _resolve_channel_mentions(text, guild):
    """Turn a plain '#channel-name' typed in the dashboard into a real <#id>
    channel link. Only replaces names that match an actual channel, so markdown
    headings ('## Title', '-# subtext') are left alone."""
    if not text or "#" not in text or not guild:
        return text
    by_name = {}
    for ch in getattr(guild, "channels", []) or []:
        nm = (getattr(ch, "name", "") or "").lower()
        if nm:
            by_name.setdefault(nm, ch.id)
    if not by_name:
        return text

    def repl(m):
        cid = by_name.get(m.group(1).lower())
        return f"<#{cid}>" if cid else m.group(0)

    return _CHANNEL_TOKEN_RE.sub(repl, text)


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
    return _resolve_emoji_shortcodes(_resolve_channel_mentions(_resolve_role_mentions(text, member.guild), member.guild), member.guild)


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
        # Custom per-service status tokens ({liveries}, {liveriesstatus}, …).
        text = _render_order_tokens(text, guild)
    return _resolve_emoji_shortcodes(_resolve_channel_mentions(_resolve_role_mentions(text, guild), guild), guild)


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


async def create_payment(method, item, price):
    """Call the payment-create edge function (holds the Roblox cookie + Stripe key)."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/payments-create",
                headers=_fn_headers(),
                json={"method": method, "item": item, "price": price},
                timeout=30,
            )
            try:
                data = r.json()
            except Exception:
                data = None
            preview = (json.dumps(data) if data is not None else (r.text or ""))[:400]
            print(f"[Payment] {method} item={item} price={price} -> HTTP {r.status_code}: {preview}")
            if isinstance(data, dict) and (data.get("ok") or data.get("error")):
                return data
            # Unexpected shape (e.g. 404 not deployed, gateway error) — surface it.
            return {"error": f"HTTP {r.status_code}: {preview or 'empty response'}"}
    except Exception as e:
        print(f"[Payment] request failed: {e!r}")
        return {"error": str(e)}


class PaymentModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create Payment", timeout=300)
        self.method = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label="Stripe (USD)", value="stripe", default=True),
            discord.SelectOption(label="Gamepass (Robux)", value="gamepass"),
            discord.SelectOption(label="Shirt (Robux)", value="shirt"),
        ])
        self.item = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label=str(i), value=str(i), default=(i == 1)) for i in range(1, 7)
        ])
        self.price = discord.ui.TextInput(
            style=discord.TextStyle.short, required=True, max_length=12,
            placeholder="e.g. 500 (Robux) or 25 (USD)",
        )
        self.add_item(discord.ui.Label(text="Method", component=self.method))
        self.add_item(discord.ui.Label(text="Item #", description="Which of your 6 gamepasses/shirts (ignored for Stripe).", component=self.item))
        self.add_item(discord.ui.Label(text="Price", description="USD for Stripe, Robux for gamepass/shirt.", component=self.price))

    async def on_submit(self, interaction):
        await interaction.response.defer(thinking=True)
        method = self.method.values[0] if self.method.values else "stripe"
        try:
            item = int(self.item.values[0]) if self.item.values else 1
        except Exception:
            item = 1
        try:
            price = float(str(self.price.value or "").replace("$", "").replace(",", "").strip())
        except Exception:
            price = 0
        result = await create_payment(method, item, price)
        if isinstance(result, dict) and result.get("ok") and result.get("url"):
            await interaction.followup.send(
                embed=success_embed("Payment ready", f"**{result.get('label', 'Payment')}**\n{result['url']}"),
            )
        else:
            err = (result or {}).get("error") if isinstance(result, dict) else str(result)
            await interaction.followup.send(embed=error_embed("Payment failed", err or "Unknown error"))


@bot.tree.command(name="payment", description="Create a payment — Stripe, gamepass, or shirt")
async def payment_cmd(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.manage_guild or has_any_role(interaction.user, ticket_config.get("support_role_ids", []))):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can create payments."), ephemeral=True)
        return
    try:
        await interaction.response.send_modal(PaymentModal())
    except Exception as e:
        print(f"[Payment] modal open failed: {e!r}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", str(e)[:300]), ephemeral=True)
        except Exception:
            pass


# ============================ Giveaways ============================

_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mo|s|m|h|d|w|y)$")
_DUR_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000}


def _parse_duration_seconds(text):
    """'30s' '10m' '2h' '1.5d' '1w' '1mo' '1y' -> seconds. Decimals are allowed
    with a unit; a bare number means DAYS (so '1' = 1 day). 0 if invalid."""
    if not text:
        return 0
    s = str(text).strip().lower()
    if s.isdigit():
        n = int(s)
        return n * 86400 if n > 0 else 0
    m = _DUR_RE.match(s)
    if not m:
        return 0
    n = float(m.group(1))
    if n <= 0:
        return 0
    return int(round(n * _DUR_MULT.get(m.group(2), 0)))


def _giveaway_can_manage(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, giveaway_config.get("manager_role_ids", []))


def _gw_cid(g, gid):
    """Enter-button custom_id carrying the giveaway's end time + winner count, so
    the bot can re-adopt a running giveaway after a redeploy without any storage."""
    try:
        return f"gw:{gid}|{int(g['end_ts'])}|{int(g['winners'])}"
    except Exception:
        return f"gw:{gid}"


def _giveaway_button(g, gid, disabled=False):
    label = str(giveaway_config.get("button_label") or "🎉 Enter")
    label, emoji = _extract_button_emoji(label)
    btn = {"type": 2, "style": 1, "custom_id": _gw_cid(g, gid), "disabled": bool(disabled)}
    if label:
        btn["label"] = label[:80]
    if emoji:
        btn["emoji"] = emoji
    return btn


def build_giveaway_embed(g, ended=False, winner_ids=None):
    prize = g["prize"]
    end_ts = int(g["end_ts"])
    entries = len(g["entrants"])
    winners = int(g["winners"])
    color = giveaway_config.get("color", ACCENT)
    try:
        color = int(color)
    except Exception:
        color = ACCENT

    title = str(giveaway_config.get("title") or "🎉 GIVEAWAY 🎉")
    lines = [f"### {prize}"]
    host_line = str(giveaway_config.get("host_line") or "").strip()
    if host_line:
        lines.append(host_line)
    lines.append("")

    if not ended:
        lines.append(f"Click **{str(giveaway_config.get('button_label') or 'Enter').strip()}** below to join!")
        lines.append(f"Ends: <t:{end_ts}:R>  •  <t:{end_ts}:f>")
    else:
        title = "🎉 GIVEAWAY ENDED 🎉"
        if winner_ids:
            mentions = ", ".join(f"<@{w}>" for w in winner_ids)
            lines.append(f"**Winner{'s' if len(winner_ids) != 1 else ''}:** {mentions}")
        else:
            lines.append("**No valid entries — no winner drawn.**")
        lines.append(f"Ended: <t:{end_ts}:f>")

    lines.append(f"Winners: **{winners}**  •  Entries: **{entries}**")
    if g.get("host_id"):
        lines.append(f"Hosted by <@{g['host_id']}>")

    embed = discord.Embed(title=title, description="\n".join(lines), color=color)
    return embed


def _giveaway_action_row(g, gid, ended=False):
    return {"type": 1, "components": [_giveaway_button(g, gid, disabled=ended)]}


def _giveaway_tokens(g, ended, winner_ids):
    end_ts = int(g["end_ts"])
    if winner_ids:
        wl = ", ".join(f"<@{w}>" for w in winner_ids)
    elif ended:
        wl = "No winners"
    else:
        wl = "TBD"
    entrants = list(g.get("entrants") or [])
    if entrants:
        participants = ", ".join(f"<@{u}>" for u in entrants)
    else:
        participants = "No one yet"
    return {
        "{prize}": g["prize"],
        "{winners}": str(int(g["winners"])),
        "{length}": str(g.get("length") or ""),
        "{entries}": str(len(g["entrants"])),
        "{participants}": participants,
        "{end}": f"<t:{end_ts}:R>",
        "{end_full}": f"<t:{end_ts}:F>",
        "{host}": f"<@{g['host_id']}>" if g.get("host_id") else "",
        "{winner_list}": wl,
        "{button}": str(giveaway_config.get("button_label") or "Enter"),
    }


_GW_BLANKLINES_RE = re.compile(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+")


def _giveaway_tidy_text(nodes):
    """After stripping {Question:} tokens, text blocks can be left with runs of
    blank lines. Collapse 3+ newlines to a single blank line and trim edges so
    the posted giveaway reads cleanly."""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == 10 and isinstance(n.get("content"), str):
            n["content"] = _GW_BLANKLINES_RE.sub("\n\n", n["content"]).strip("\n")
        for key in ("components", "items"):
            if isinstance(n.get(key), list):
                _giveaway_tidy_text(n[key])


def _giveaway_render_design(g, gid, guild, ended=False, winner_ids=None):
    """Render a giveaway layout with tokens filled in. While running (or when no
    dedicated ended design exists) uses the running design + Enter button. When
    ended AND a separate ended design is configured, uses that + a Reroll button.
    Returns None if no design is configured."""
    running_design = g.get("design") or giveaway_config.get("components") or []
    ended_design = giveaway_config.get("ended_components") or []
    # Only swap to the ended design if the running message was also a V2 design —
    # otherwise the posted message is an embed and can't be edited into V2.
    use_ended_design = bool(ended and ended_design and running_design)
    design = ended_design if use_ended_design else running_design
    if not design:
        return None

    def _js(x):
        return json.dumps(str(x))[1:-1]

    raw = json.dumps(design)
    for tok, val in _giveaway_tokens(g, ended, winner_ids).items():
        raw = raw.replace(tok, _js(val))
    # {Question: LABEL} is only the QUESTION (it defines the /giveaway form field).
    # It shows nothing in the posted message — the ANSWER shows via {prize} etc.
    raw = _QUESTION_RE.sub("", raw)
    try:
        comps = json.loads(raw)
    except Exception:
        comps = design

    built = [b for b in (_build_v2(c, guild) for c in comps) if b]
    _giveaway_tidy_text(built)

    if use_ended_design:
        # Dedicated ended message: winner text only, no buttons. Staff reroll via
        # the -reroll command. Never allow an empty payload — a blank edit would
        # make the message look "deleted" — so fall back to a minimal winner line.
        if not built:
            wl = ", ".join(f"<@{w}>" for w in (winner_ids or [])) or "No winners"
            built = [{"type": 10, "content": f"**Giveaway ended.** Winner: {wl}"}]
        return built

    # Bind any user-placed Counter buttons to THIS giveaway and disable them once
    # it's ended. If the design has none, append the default Enter row.
    def _bind_counter(node):
        found = False
        if isinstance(node, dict):
            if node.get("type") == 2 and str(node.get("custom_id", "")).startswith("gw:__COUNTER__"):
                node["custom_id"] = _gw_cid(g, gid)
                if ended:
                    node["disabled"] = True
                found = True
            for v in node.get("components", []) or []:
                found = _bind_counter(v) or found
        return found

    has_counter = False
    for c in built:
        has_counter = _bind_counter(c) or has_counter

    ping = str(giveaway_config.get("ping") or "").strip()
    if ping and not ended:
        built.insert(0, {"type": 10, "content": _render_guild_text(ping, guild)})
    if not has_counter:
        # No user-placed entry button — add the default Enter row (disabled on end).
        built.append(_giveaway_action_row(g, gid, ended))
    return built


def _giveaway_render_guard(built, g, gid, ended, winner_ids):
    """Never return an empty/whitespace-only render — a blank edit makes the
    posted giveaway look deleted. Guarantees at least one visible component."""
    real = [c for c in (built or []) if isinstance(c, dict)]
    if real:
        return built
    if ended:
        wl = ", ".join(f"<@{w}>" for w in (winner_ids or [])) or "No winners"
        return [{"type": 10, "content": f"**Giveaway ended.** Winner: {wl}"}]
    return [{"type": 10, "content": "**Giveaway** — click below to enter!"},
            _giveaway_action_row(g, gid, False)]


def _giveaway_payload(g, gid, guild, ended=False, winner_ids=None, for_edit=False):
    """Build the message payload for a giveaway. Uses the designed V2 layout when
    one exists, otherwise the built-in embed."""
    design = _giveaway_render_design(g, gid, guild, ended, winner_ids)
    if design is not None:
        design = _giveaway_render_guard(design, g, gid, ended, winner_ids)
        payload = {"components": design}
        if not for_edit:
            payload["flags"] = 1 << 15  # Components V2
            payload["allowed_mentions"] = {"parse": ["roles", "users"]}
        return payload
    embed = build_giveaway_embed(g, ended=ended, winner_ids=winner_ids)
    payload = {"embeds": [embed.to_dict()], "components": [_giveaway_action_row(g, gid, ended)]}
    if not for_edit:
        payload["allowed_mentions"] = {"parse": ["roles", "users"]}
        ping = str(giveaway_config.get("ping") or "").strip()
        if ping:
            payload["content"] = _render_guild_text(ping, guild)
    return payload


async def _giveaway_send(channel, g, gid):
    guild = getattr(channel, "guild", None)
    payload = _giveaway_payload(g, gid, guild, ended=False)
    route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    resp = await bot.http.request(route, json=payload)
    return str(resp["id"]) if isinstance(resp, dict) and resp.get("id") else None


async def _giveaway_patch(g, payload):
    try:
        route = discord.http.Route(
            "PATCH", "/channels/{channel_id}/messages/{message_id}",
            channel_id=int(g["channel_id"]), message_id=int(g["message_id"]),
        )
        await bot.http.request(route, json=payload)
    except Exception as e:
        print(f"[Giveaway] edit failed: {e}")


async def _giveaway_refresh_count(gid):
    g = active_giveaways.get(gid)
    if not g or g.get("ended"):
        return
    channel = await resolve_channel(g["channel_id"])
    guild = getattr(channel, "guild", None) if channel else None
    await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=False, for_edit=True))


def _pick_winners(entrants, count):
    pool = [e for e in entrants]
    if not pool:
        return []
    return random.sample(pool, min(count, len(pool)))


async def start_giveaway(channel, prize, winners, seconds, host_id, guild_id, design=None, length=""):
    gid = secrets.token_hex(6)
    end_ts = int(time.time()) + seconds
    g = {
        "message_id": None, "channel_id": str(channel.id), "guild_id": str(guild_id or ""),
        "prize": prize, "winners": max(1, int(winners)), "end_ts": end_ts, "length": length or "",
        "host_id": str(host_id or ""), "entrants": set(), "ended": False,
        # Optional per-giveaway design override; normally None (uses the shared
        # dashboard design, with answer tokens filled from this giveaway's values).
        "design": design if isinstance(design, list) and design else None,
    }
    active_giveaways[gid] = g
    mid = await _giveaway_send(channel, g, gid)
    if not mid:
        active_giveaways.pop(gid, None)
        return None
    g["message_id"] = mid
    await _gw_save_state(gid, g)  # persist so it survives a redeploy immediately
    asyncio.create_task(_giveaway_timer(gid, seconds))
    return gid


async def _giveaway_timer(gid, seconds):
    try:
        await asyncio.sleep(max(1, seconds))
    except asyncio.CancelledError:
        return
    await end_giveaway(gid)


async def end_giveaway(gid, actor_id=None):
    g = active_giveaways.get(gid)
    if not g or g.get("ended"):
        return None
    g["ended"] = True
    winner_ids = _pick_winners(g["entrants"], g["winners"])
    g["last_winners"] = winner_ids
    channel = await resolve_channel(g["channel_id"])
    guild = getattr(channel, "guild", None) if channel else None
    # Edit the giveaway message in place to show the winner. No separate
    # congratulations message is posted — the winner shows on the message itself,
    # which is never deleted.
    await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=True, winner_ids=winner_ids, for_edit=True))
    await _gw_save_state(gid, g)  # persist the ended state + winners for reroll after a redeploy
    print(f"[Giveaway] {gid} ended — message {g.get('message_id')} EDITED to winner state (never deleted)")
    return winner_ids


def _giveaway_params_from_answers(labels, mapping):
    """Work out prize / winner-count / duration (and the raw length text) from the
    {Question:} answers by matching label keywords. A {Question:} token is only a
    QUESTION — it defines a form field; the ANSWER shows via {prize}/{winners}/etc."""
    WINNER_KW = ("winner", "how many")
    LENGTH_KW = ("length", "duration", "how long")
    prize, winners, seconds, length_str = "", 1, 0, ""
    prize_set = False
    for lbl in labels:
        ans = (mapping.get(lbl) or "").strip()
        low = lbl.lower()
        if any(k in low for k in WINNER_KW):
            try:
                winners = max(1, min(int(re.sub(r"[^0-9]", "", ans) or "1"), 50))
            except Exception:
                winners = 1
        elif any(k in low for k in LENGTH_KW):
            seconds = _parse_duration_seconds(ans)
            length_str = ans
        elif "prize" in low and not prize_set:
            prize, prize_set = ans, True
    if not prize_set:
        # No explicit prize question — use the first non-winner/non-length answer.
        for lbl in labels:
            low = lbl.lower()
            if any(k in low for k in WINNER_KW + LENGTH_KW):
                continue
            if (mapping.get(lbl) or "").strip():
                prize = mapping[lbl].strip()
                break
    if not seconds:
        seconds = _parse_duration_seconds(str(giveaway_config.get("default_duration") or "1d")) or 86400
    return prize, winners, seconds, length_str


async def handle_giveaway_form_submit(interaction):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception as e:
        print(f"[Giveaway] form submit defer failed: {e}")
    try:
        design = giveaway_config.get("components") or []
        labels = _parse_questions(design)
        vals = _collect_modal_values((interaction.data or {}).get("components"))
        mapping = {lbl: (vals.get(f"q{i}") or "").strip() for i, lbl in enumerate(labels)}
        prize, winners, seconds, length_str = _giveaway_params_from_answers(labels, mapping)
        gid = await start_giveaway(
            interaction.channel, prize, winners, seconds,
            host_id=interaction.user.id, guild_id=getattr(interaction.guild, "id", None),
            length=length_str,
        )
        if gid:
            await interaction.followup.send(embed=success_embed("Giveaway started", f"Ends <t:{int(time.time()) + seconds}:R> — {winners} winner(s)."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed("Couldn't post", "I couldn't post the giveaway here. Check my permissions in this channel."), ephemeral=True)
    except Exception as e:
        import traceback
        print(f"[Giveaway] form submit failed: {e}\n{traceback.format_exc()}")
        try:
            await interaction.followup.send(embed=error_embed("Couldn't start giveaway", "Something went wrong. Please try again."), ephemeral=True)
        except Exception:
            pass


async def _open_giveaway_question_form(interaction, questions):
    components = []
    for i, q in enumerate(questions):
        components.append({
            "type": 18,  # Label
            "label": (_clean_label(q) or q)[:45],
            "component": {
                "type": 4, "custom_id": f"q{i}", "style": _form_input_style(q),
                "required": True, "max_length": 1000,
            },
        })
    data = {"title": "Start Giveaway", "custom_id": "giveawayform", "components": components}
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={"type": 9, "data": data})


class GiveawayModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Start Giveaway", timeout=300)
        self.prize = discord.ui.TextInput(
            label="Prize", style=discord.TextStyle.short, required=True, max_length=200,
            placeholder="Discord Nitro, $20 gift card, 1000 Robux…",
        )
        self.winners = discord.ui.TextInput(
            label="Winner(s)", style=discord.TextStyle.short, required=False, max_length=3,
            default=str(giveaway_config.get("default_winners", 1)),
            placeholder="How many winners (e.g. 1)",
        )
        self.length = discord.ui.TextInput(
            label="Length", style=discord.TextStyle.short, required=True, max_length=8,
            default=str(giveaway_config.get("default_duration", "1d")),
            placeholder="30s, 10m, 2h, 1d, 1w (or just a number = days)",
        )
        self.add_item(self.prize)
        self.add_item(self.winners)
        self.add_item(self.length)

    async def on_submit(self, interaction):
        prize = str(self.prize.value or "").strip()
        try:
            winners = int(str(self.winners.value or "1").strip() or "1")
        except Exception:
            winners = 1
        winners = max(1, min(winners, 50))
        seconds = _parse_duration_seconds(self.length.value)
        if not prize:
            await interaction.response.send_message(embed=error_embed("Prize required", "Enter what you're giving away."), ephemeral=True)
            return
        if not seconds:
            await interaction.response.send_message(embed=error_embed("Invalid length", "Use a format like 10m, 2h, 1d, 1w, or 1mo."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        gid = await start_giveaway(
            interaction.channel, prize, winners, seconds,
            host_id=interaction.user.id, guild_id=getattr(interaction.guild, "id", None),
            length=str(self.length.value or "").strip(),
        )
        if gid:
            await interaction.followup.send(embed=success_embed("Giveaway started", f"**{prize}** — {winners} winner(s), ends <t:{int(time.time()) + seconds}:R>."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed("Couldn't post", "I couldn't post the giveaway here. Check my permissions in this channel."), ephemeral=True)


@bot.tree.command(name="giveaway", description="Start a giveaway — prize, winners, and length")
async def giveaway_cmd(interaction: discord.Interaction):
    if not _giveaway_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can start giveaways."), ephemeral=True)
        return
    # If the design defines {Question:} fields, the form is built from those.
    # Otherwise fall back to the standard Prize / Winner(s) / Length modal.
    questions = _parse_questions(giveaway_config.get("components") or [])
    try:
        if questions:
            await _open_giveaway_question_form(interaction, questions)
        else:
            await interaction.response.send_modal(GiveawayModal())
    except Exception as e:
        print(f"[Giveaway] modal open failed: {e!r}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", str(e)[:300]), ephemeral=True)
        except Exception:
            pass


# ============================ Robux Locker (stocking) ============================


def _robux_can_manage(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, ticket_config.get("support_role_ids", []))


def _modal_values(components):
    """Flatten a modal_submit tree into {custom_id: value}, handling both text
    inputs (value) and selects (values[0])."""
    out = {}
    for row in components or []:
        if not isinstance(row, dict):
            continue
        inner = row.get("component")
        cands = [inner] if isinstance(inner, dict) else []
        cands += [c for c in (row.get("components") or []) if isinstance(c, dict)]
        for c in cands:
            cid = c.get("custom_id")
            if not cid:
                continue
            if "values" in c:
                vals = c.get("values") or []
                out[cid] = vals[0] if vals else ""
            else:
                out[cid] = c.get("value", "") or ""
    return out


@bot.tree.command(name="robuxlocker", description="Stock the Robux Locker from the group's funds")
async def robuxlocker_cmd(interaction: discord.Interaction):
    if not _robux_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can stock the locker."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    res = await _robux_locker_call("funds")
    if not (isinstance(res, dict) and res.get("ok")):
        err = (res or {}).get("error", "Unknown error")
        await interaction.followup.send(embed=error_embed("Couldn't read group funds", str(err)[:400]), ephemeral=True)
        return
    funds = int(res.get("robux") or 0)
    robux_locker_config["last_funds"] = funds
    server_name = getattr(interaction.guild, "name", "The group")
    row = {"type": 1, "components": [{
        "type": 2, "style": 1, "custom_id": f"robuxstock:{funds}", "label": "Enter amount",
    }]}
    # discord.py's followup.send doesn't take raw components, so send the button
    # via the raw interaction webhook (flags: ephemeral).
    try:
        route = discord.http.Route("POST", "/webhooks/{application_id}/{interaction_token}",
                                   application_id=bot.application_id, interaction_token=interaction.token)
        await bot.http.request(route, json={
            "content": f"**{server_name}** has **{funds:,}** Robux available. How many would you like to use?",
            "components": [row],
            "flags": 1 << 6,
        })
    except Exception as e:
        print(f"[RobuxLocker] funds prompt failed: {e}")
        await interaction.followup.send(embed=info_embed("Group funds", f"Available: **{funds:,}** Robux. Run the command again to stock."), ephemeral=True)


class RobuxRateModal(discord.ui.Modal):
    """Set the sell rate — USD charged per 1,000 Robux."""
    def __init__(self, current=0.0):
        super().__init__(title="Robux Locker Rate", timeout=300)
        self.rate = discord.ui.TextInput(
            style=discord.TextStyle.short, required=True, max_length=12,
            placeholder="e.g. $7",
            default=(f"{current:g}" if current else ""),
        )
        self.add_item(discord.ui.Label(
            text="Rate per 1,000 Robux (USD)",
            description="For every 1,000 Robux, what does a member pay? e.g. $7",
            component=self.rate,
        ))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            rate = float(re.sub(r"[^0-9.]", "", str(self.rate.value or "0")) or "0")
        except Exception:
            rate = 0.0
        if rate <= 0:
            await interaction.followup.send(embed=error_embed("Invalid rate", "Enter a dollar amount above 0, like $7."), ephemeral=True)
            return
        res = await _robux_locker_call("set_rate", rate)
        if not (isinstance(res, dict) and res.get("ok")):
            err = (res or {}).get("error", "Unknown error")
            await interaction.followup.send(embed=error_embed("Couldn't save the rate", str(err)[:400]), ephemeral=True)
            return
        robux_locker_config["rate_per_1k"] = float(res.get("rate_per_1k") or rate)
        r = robux_locker_config["rate_per_1k"]
        await interaction.followup.send(
            embed=success_embed("Rate saved", f"Members now pay **${r:,.2f}** per **1,000** Robux.\nExample: 1,000 Robux = **${r:,.2f}**, 5,000 = **${r*5:,.2f}**."),
            ephemeral=True)


@bot.tree.command(name="robuxlockerrate", description="Set the Robux sell rate (USD per 1,000 Robux)")
async def robuxlockerrate_cmd(interaction: discord.Interaction):
    if not _robux_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can set the rate."), ephemeral=True)
        return
    try:
        await interaction.response.send_modal(RobuxRateModal(float(robux_locker_config.get("rate_per_1k") or 0)))
    except Exception as e:
        print(f"[RobuxLocker] rate modal open failed: {e!r}")


@bot.tree.command(name="funds", description="Group funds — available and pending")
@app_commands.describe(period="Revenue window for the breakdown (default: this month)")
@app_commands.choices(period=[
    app_commands.Choice(name="Today", value="Day"),
    app_commands.Choice(name="This Week", value="Week"),
    app_commands.Choice(name="This Month", value="Month"),
    app_commands.Choice(name="This Year", value="Year"),
])
async def funds_cmd(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    if not _robux_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can view group funds."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    tf = period.value if period else "Month"
    res = await _robux_locker_call("funds_detail", time_frame=tf)
    if not (isinstance(res, dict) and res.get("ok")):
        err = (res or {}).get("error", "Unknown error")
        await interaction.followup.send(embed=error_embed("Couldn't read funds", str(err)[:400]), ephemeral=True)
        return
    available = int(res.get("available") or 0)
    pending = res.get("pending")

    e = discord.Embed(title="Group Funds")
    e.add_field(name="Available", value=f"{available:,} R$", inline=True)
    if pending is not None:
        e.add_field(name="Pending", value=f"{int(pending):,} R$", inline=True)
    else:
        e.add_field(name="Pending", value="Unavailable", inline=True)
        err = str(res.get("summaryError") or "no detail returned")
        e.add_field(name="​", value=f"Pending couldn't load:\n`{err[:400]}`", inline=False)
    e.set_footer(text="Available = spendable now · Pending = held from recent sales")
    await interaction.followup.send(embed=e, ephemeral=True)


async def _open_robux_stock_modal(interaction, funds):
    components = [
        {"type": 18, "label": "Amount (Robux)", "description": f"Available: {funds:,}. Can't exceed this.",
         "component": {"type": 4, "custom_id": "amount", "style": 1, "required": True, "max_length": 12, "placeholder": "e.g. 1000"}},
        {"type": 18, "label": "Confirm",
         "component": {"type": 3, "custom_id": "confirm", "min_values": 1, "max_values": 1, "options": [
             {"label": "No — cancel", "value": "no", "default": True},
             {"label": "Yes — add to Available Stock", "value": "yes"},
         ]}},
    ]
    data = {"title": "Stock the Robux Locker", "custom_id": f"robuxstockform:{funds}", "components": components}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 9, "data": data})


async def handle_robux_stock_submit(interaction, funds):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    try:
        vals = _modal_values((interaction.data or {}).get("components"))
        if (vals.get("confirm") or "no") != "yes":
            await interaction.followup.send(embed=info_embed("Cancelled", "Nothing was added to Available Stock."), ephemeral=True)
            return
        try:
            amount = int(re.sub(r"[^0-9]", "", str(vals.get("amount") or "0")) or "0")
        except Exception:
            amount = 0
        if amount <= 0:
            await interaction.followup.send(embed=error_embed("Invalid amount", "Enter a number above 0."), ephemeral=True)
            return
        if amount > int(funds):
            await interaction.followup.send(embed=error_embed("Too high", f"You only have **{int(funds):,}** Robux available."), ephemeral=True)
            return
        res = await _robux_locker_call("add_stock", amount)
        if not (isinstance(res, dict) and res.get("ok")):
            err = (res or {}).get("error", "Unknown error")
            await interaction.followup.send(embed=error_embed("Couldn't update stock", str(err)[:400]), ephemeral=True)
            return
        robux_locker_config["stock"] = int(res.get("stock") or 0)
        await _robux_update_panel()
        await interaction.followup.send(embed=success_embed("Stocked", f"Added **{amount:,}** Robux. Available Stock is now **{robux_locker_config['stock']:,}**."), ephemeral=True)
    except Exception as e:
        import traceback
        print(f"[RobuxLocker] stock submit failed: {e}\n{traceback.format_exc()}")
        try:
            await interaction.followup.send(embed=error_embed("Something went wrong", "Please try again."), ephemeral=True)
        except Exception:
            pass


# ---- Robux Locker: member "Buy Robux" flow ----

async def _open_robux_buy_modal(interaction):
    """A member clicked Buy Robux — ask how much + how they'll pay. Stock is
    re-checked authoritatively when they submit (first come, first served)."""
    components = [
        {"type": 18, "label": "How much Robux?",
         "component": {"type": 4, "custom_id": "amount", "style": 1, "required": True, "max_length": 12, "placeholder": "e.g. 1000"}},
    ]
    data = {"title": "Buy Robux", "custom_id": "robuxbuyform", "components": components}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 9, "data": data})


async def handle_robux_buy_click(interaction):
    # The button is rendered disabled when stock is 0, but the panel can be
    # stale — so if we already know stock is 0, refuse fast; otherwise open the
    # form and let take_stock be the real gate on submit.
    if int(robux_locker_config.get("stock") or 0) <= 0:
        try:
            await interaction.response.send_message(embed=error_embed("Out of stock", "There's no Robux available right now. Check back soon."), ephemeral=True)
        except Exception:
            pass
        return
    try:
        await _open_robux_buy_modal(interaction)
    except Exception as e:
        print(f"[RobuxLocker] buy modal open failed: {e}")


async def handle_robux_buy_submit(interaction):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    reserved = 0
    try:
        vals = _modal_values((interaction.data or {}).get("components"))
        # Robux is sold for money only — you can't pay for Robux with Robux, so
        # the only method is Stripe (card).
        method = "stripe"
        try:
            amount = int(re.sub(r"[^0-9]", "", str(vals.get("amount") or "0")) or "0")
        except Exception:
            amount = 0
        if amount <= 0:
            await interaction.followup.send(embed=error_embed("Invalid amount", "Enter a number above 0."), ephemeral=True)
            return
        # Price it from the sell rate (USD per 1,000 Robux). No rate = not for
        # sale yet — staff must run /robuxlockerrate first.
        rate = float(robux_locker_config.get("rate_per_1k") or 0)
        if rate <= 0:
            await interaction.followup.send(embed=error_embed("Not for sale yet", "Pricing isn't set. A staff member needs to run `/robuxlockerrate` first."), ephemeral=True)
            return
        price = round(amount / 1000.0 * rate, 2)
        if price <= 0:
            await interaction.followup.send(embed=error_embed("Amount too small", "That works out to $0.00 — buy a larger amount."), ephemeral=True)
            return
        # Reserve the stock FIRST — first come, first served. If someone already
        # took it, take_stock returns ok:false and nothing is reserved.
        res = await _robux_locker_call("take_stock", amount)
        if not (isinstance(res, dict) and res.get("ok")):
            have = int((res or {}).get("stock") or 0)
            await interaction.followup.send(
                embed=error_embed("Not enough left", f"Only **{have:,}** Robux is available right now — someone may have just bought some."),
                ephemeral=True)
            return
        reserved = amount
        robux_locker_config["stock"] = int(res.get("stock") or 0)
        await _robux_update_panel()
        # Build the Stripe payment link priced from the rate.
        pay = await create_payment(method, 1, price)
        if isinstance(pay, dict) and pay.get("ok") and pay.get("url"):
            reserved = 0  # committed — don't refund
            await interaction.followup.send(
                embed=success_embed("You're first in line", f"**{amount:,} Robux** reserved for you — **${price:,.2f}**.\n{pay['url']}\n\nComplete payment to claim it — it's first come, first served."),
                ephemeral=True)
        else:
            # Payment link failed — release the reservation back to stock.
            err = (pay or {}).get("error") if isinstance(pay, dict) else str(pay)
            await _robux_locker_call("add_stock", reserved)
            reserved = 0
            robux_locker_config["stock"] = int(robux_locker_config.get("stock") or 0) + amount
            await _robux_update_panel()
            await interaction.followup.send(embed=error_embed("Payment failed", str(err or "Unknown error")[:400]), ephemeral=True)
    except Exception as e:
        import traceback
        print(f"[RobuxLocker] buy submit failed: {e}\n{traceback.format_exc()}")
        if reserved:
            try:
                await _robux_locker_call("add_stock", reserved)
            except Exception:
                pass
        try:
            await interaction.followup.send(embed=error_embed("Something went wrong", "Please try again."), ephemeral=True)
        except Exception:
            pass


async def handle_notify_click(interaction, ids_csv):
    """Notification button — toggle the selected role(s) on the clicker."""
    guild = interaction.guild
    member = getattr(interaction, "user", None)
    if not (guild and isinstance(member, discord.Member)):
        try:
            await interaction.response.send_message(embed=error_embed("Unavailable", "This only works inside a server."), ephemeral=True)
        except Exception:
            pass
        return
    roles = []
    for rid in str(ids_csv).split(","):
        rid = rid.strip()
        if rid.isdigit():
            r = guild.get_role(int(rid))
            if r:
                roles.append(r)
    if not roles:
        try:
            await interaction.response.send_message(embed=error_embed("Not set up", "No roles are attached to this button."), ephemeral=True)
        except Exception:
            pass
        return
    added, removed = [], []
    try:
        for r in roles:
            if r in member.roles:
                await member.remove_roles(r, reason="Notification button")
                removed.append(r.mention)
            else:
                await member.add_roles(r, reason="Notification button")
                added.append(r.mention)
    except discord.Forbidden:
        try:
            await interaction.response.send_message(embed=error_embed("Missing permission", "I can't manage that role — make sure my role is above it."), ephemeral=True)
        except Exception:
            pass
        return
    except Exception as e:
        print(f"[Notify] toggle failed: {e}")
    parts = []
    if added:
        parts.append("Added " + ", ".join(added))
    if removed:
        parts.append("Removed " + ", ".join(removed))
    msg = " · ".join(parts) if parts else "No changes."
    try:
        await interaction.response.send_message(embed=success_embed("Notifications", msg), ephemeral=True)
    except Exception:
        pass


async def show_order_status(interaction):
    """Order Status button — live embed of each service's open/limited/closed
    state based on how many order tickets are open in its category."""
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    guild = interaction.guild
    if not guild:
        await interaction.followup.send(embed=error_embed("Unavailable", "This only works inside a server."), ephemeral=True)
        return
    services = order_status_config.get("services") or []
    lines = []
    for svc in services:
        name = svc.get("name") or ""
        emoji, lbl = _order_status_for(guild, svc)
        emoji = (emoji or "").strip()
        # 'Name — <emoji>' (no status word). Falls back to the word if no emoji.
        lines.append(f"**{name}** — {emoji}" if emoji else f"**{name}** — {lbl}")

    title = order_status_config.get("title") or "Order Status"
    desc = "\n".join(lines) if lines else "No services are configured yet."
    # Resolve any :emoji: shortcodes in the assembled text.
    e = discord.Embed(title=_render_guild_text(title, guild), description=_render_guild_text(desc, guild))
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="status", description="Show the current order status")
async def status_cmd(interaction: discord.Interaction):
    await show_order_status(interaction)


# ===================== Portfolio =====================

async def _portfolio_posts_call(action, thread_id=None, channel_id=None,
                                guild_id=None, owner_id=None, owner_name=None):
    """Persist/read portfolio-post ownership server-side so posts survive
    redeploys and a daily sweep can delete a post when its owner leaves.
    Actions: get_all (no thread_id), add, remove."""
    payload = {"action": action}
    if thread_id is not None:
        payload["thread_id"] = str(thread_id)
    if channel_id is not None:
        payload["channel_id"] = str(channel_id)
    if guild_id is not None:
        payload["guild_id"] = str(guild_id)
    if owner_id is not None:
        payload["owner_id"] = str(owner_id)
    if owner_name is not None:
        payload["owner_name"] = str(owner_name)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/portfolio-posts",
                headers=_fn_headers(), json=payload, timeout=15,
            )
            data = r.json() if r.content else {}
            if r.status_code != 200 or (isinstance(data, dict) and data.get("error")):
                # A 404 here means the 'portfolio-posts' edge function isn't deployed.
                print(f"[Portfolio] posts {action} -> HTTP {r.status_code}: {str(data)[:200]}")
            return data
    except Exception as e:
        print(f"[Portfolio] posts {action} call failed: {e}")
        return {"error": str(e)[:200]}


async def _portfolio_find_existing(guild_id, owner_id):
    """Return the thread id of this member's existing portfolio post in this
    guild, or None. Cleans up records whose thread was already deleted."""
    res = await _portfolio_posts_call("get_all")
    if not (isinstance(res, dict) and res.get("ok")):
        return None
    for tid, rec in (res.get("posts") or {}).items():
        if str(rec.get("guild_id")) != str(guild_id) or str(rec.get("owner_id")) != str(owner_id):
            continue
        # Make sure the post still exists — if it was deleted, forget it so a
        # fresh one can be made.
        try:
            ch = bot.get_channel(int(tid)) or await bot.fetch_channel(int(tid))
        except discord.NotFound:
            ch = None
        except Exception:
            return tid  # can't verify right now — treat as existing to be safe
        if ch is None:
            await _portfolio_posts_call("remove", thread_id=tid)
            continue
        return tid
    return None


async def _do_portfolio_post(interaction):
    """Create this member's portfolio post. Assumes the interaction is already
    deferred (ephemeral) and replies with a followup. Shared by /portfolio and
    /joinsetup. Enforces one live post per member in forum channels."""
    comps = portfolio_config.get("components") or []
    if not comps:
        await interaction.followup.send(embed=error_embed("Nothing to post", "Design the portfolio in the dashboard first, then save it."), ephemeral=True)
        return
    ch = await resolve_channel(portfolio_config.get("channel_id"))
    if not ch:
        await interaction.followup.send(embed=error_embed("No channel", "Pick a channel for the portfolio in the dashboard, then save it."), ephemeral=True)
        return
    _V2_LAST_ERROR["msg"] = ""
    # Forum channels can't take a plain message — post a new thread (forum post)
    # named after the member, and remember who owns it.
    if isinstance(ch, discord.ForumChannel):
        # One portfolio post per member — if they already have a live one, just
        # hand them the link instead of making a second.
        existing = await _portfolio_find_existing(interaction.guild_id, interaction.user.id)
        if existing:
            link = f"https://discord.com/channels/{interaction.guild_id}/{existing}"
            await interaction.followup.send(embed=info_embed("You already have a portfolio", f"Here's yours: {link}"), ephemeral=True)
            return
        mid = await send_v2_forum_post(ch, comps, name=interaction.user.name)
        if mid and mid is not True:
            await _portfolio_posts_call(
                "add", thread_id=mid, channel_id=ch.id,
                guild_id=interaction.guild_id, owner_id=interaction.user.id,
                owner_name=interaction.user.name,
            )
            link = f"https://discord.com/channels/{interaction.guild_id}/{mid}"
            await interaction.followup.send(embed=success_embed("Posted", f"Your portfolio is up: {link}"), ephemeral=True)
        else:
            reason = _V2_LAST_ERROR.get("msg") or "unknown error"
            await interaction.followup.send(embed=error_embed("Couldn't post", f"Discord rejected the portfolio: {reason}"), ephemeral=True)
        return
    if isinstance(ch, discord.CategoryChannel) or not hasattr(ch, "send"):
        await interaction.followup.send(embed=error_embed("Not postable", "The portfolio channel is a category. Pick a text or forum channel in the dashboard, then save it."), ephemeral=True)
        return
    mid = await send_v2_message(ch, comps)
    if mid:
        await interaction.followup.send(embed=success_embed("Posted", f"Portfolio posted in {ch.mention}."), ephemeral=True)
    else:
        reason = _V2_LAST_ERROR.get("msg") or "unknown error"
        await interaction.followup.send(embed=error_embed("Couldn't post", f"Discord rejected the portfolio: {reason}"), ephemeral=True)


def _portfolio_can_use(member):
    """Manage Server, or one of the roles picked in the dashboard Portfolio block."""
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, portfolio_config.get("allowed_role_ids", []))


@bot.tree.command(name="portfolio", description="Post your portfolio to its channel")
async def portfolio_cmd(interaction: discord.Interaction):
    if not _portfolio_can_use(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "You don't have a role allowed to run /portfolio."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _do_portfolio_post(interaction)


@tasks.loop(hours=24)
async def portfolio_cleanup():
    """Once a day, delete any portfolio post whose owner has left the server.
    Also forgets records whose post was already deleted by hand."""
    res = await _portfolio_posts_call("get_all")
    if not (isinstance(res, dict) and res.get("ok")):
        return
    posts = res.get("posts") or {}
    for tid, rec in list(posts.items()):
        guild_id = rec.get("guild_id")
        owner_id = rec.get("owner_id")
        guild = bot.get_guild(int(guild_id)) if guild_id else None
        if not guild:
            continue  # bot isn't in that guild right now — can't verify, leave it
        # Is the post itself still there?
        try:
            thread = bot.get_channel(int(tid)) or await bot.fetch_channel(int(tid))
        except discord.NotFound:
            thread = None
        except Exception:
            continue  # transient error — try again next sweep
        if thread is None:
            await _portfolio_posts_call("remove", thread_id=tid)
            continue
        # Is the owner still a member?
        present = None
        if owner_id:
            if guild.get_member(int(owner_id)) is not None:
                present = True
            else:
                try:
                    await guild.fetch_member(int(owner_id))
                    present = True
                except discord.NotFound:
                    present = False
                except Exception:
                    present = None  # transient — skip this cycle
        if present is False:
            try:
                await thread.delete()
                print(f"[Portfolio] deleted post {tid} — owner {owner_id} left")
            except Exception as e:
                print(f"[Portfolio] cleanup delete failed for {tid}: {e}")
            await _portfolio_posts_call("remove", thread_id=tid)


@portfolio_cleanup.before_loop
async def before_portfolio_cleanup():
    await bot.wait_until_ready()


# ===================== Pricing =====================

async def _pricing_call(action, entries=None):
    """POST to the pricing edge function (get / set price values)."""
    payload = {"action": action}
    if entries is not None:
        payload["entries"] = entries
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/pricing",
                headers=_fn_headers(), json=payload, timeout=20,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200:
                return data
            # A 404 here means the 'pricing' edge function isn't deployed.
            print(f"[Pricing] {action} -> HTTP {r.status_code}: {str(data)[:200]}")
            return {"error": data.get("error") or f"HTTP {r.status_code}"}
    except Exception as e:
        print(f"[Pricing] {action} call failed: {e}")
        return {"error": str(e)[:200]}


def _pricing_can_manage(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, pricing_config.get("designer_role_ids", []))


async def _raw_interaction_reply(interaction, resp_type, content=None, embeds=None, components=None, ephemeral=True):
    """Reply to an interaction with raw components/embeds. resp_type 4 = new
    message (command), 7 = update the existing message (component click)."""
    data = {}
    if ephemeral and resp_type == 4:
        data["flags"] = 1 << 6
    if content is not None:
        data["content"] = content
    if embeds is not None:
        data["embeds"] = embeds
    if components is not None:
        data["components"] = components
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={"type": resp_type, "data": data})


def _pricing_service_select(custom_id):
    services = pricing_config.get("services") or []
    options = [{"label": s["name"][:100], "value": str(i)} for i, s in enumerate(services[:25])]
    return {"type": 1, "components": [{
        "type": 3, "custom_id": custom_id, "placeholder": "Choose a service", "options": options,
    }]}


def _price_parts(val):
    """Return (robux, usd) strings for a stored item value. New values are
    {robux, usd}; a legacy plain string is treated as the USD price."""
    if isinstance(val, dict):
        return str(val.get("robux") or "").strip(), str(val.get("usd") or "").strip()
    return "", str(val or "").strip()


def _pricing_lines_text(si, guild=None):
    """Fills {pricing}: for one service, each DESIGNER's block — their @mention
    then their priced items — ordered by who joined the server first."""
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return ""
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    cur = pricing_config.get("currency") or "$"
    by_user = (pricing_config.get("values") or {}).get(name, {})  # {user_id: {item: {robux,usd}}}
    if not isinstance(by_user, dict) or not by_user:
        return "No pricing set yet."

    def _join_key(uid):
        # Earliest server join first; members not found go last.
        m = guild.get_member(int(uid)) if (guild and str(uid).isdigit()) else None
        joined = getattr(m, "joined_at", None) if m else None
        return (0, joined.timestamp()) if joined else (1, str(uid))

    blocks = []
    for uid in sorted(by_user.keys(), key=_join_key):
        item_map = by_user.get(uid) or {}
        if not isinstance(item_map, dict):
            continue
        lines = []
        for item in items:
            robux, usd = _price_parts(item_map.get(item))
            parts = []
            if robux:
                parts.append(f"R$ {robux}")
            if usd:
                parts.append(f"{cur}{usd}")
            if parts:  # only items this designer actually priced
                lines.append(f"{item} — {' · '.join(parts)}")
        if lines:
            blocks.append(f"<@{uid}>\n" + "\n".join(lines))
    return "\n\n".join(blocks) if blocks else "No pricing set yet."


def _pricing_embed(si, guild=None):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return None
    name = services[si].get("name") or ""
    title = pricing_config.get("title") or "Pricing"
    return discord.Embed(title=f"{title} · {name}",
                         description=_render_guild_text(_pricing_lines_text(si, guild), guild))


def _render_pricing_components(si, guild=None):
    """The dashboard-designed /pricing layout with {service} and {pricing}
    substituted for this service. None if no design is saved."""
    comps = pricing_config.get("components") or []
    if not comps:
        return None
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return None
    name = services[si].get("name") or ""
    raw = json.dumps(comps)
    raw = raw.replace("{pricing}", json.dumps(_pricing_lines_text(si, guild))[1:-1])
    raw = raw.replace("{service}", json.dumps(name)[1:-1])
    try:
        return json.loads(raw)
    except Exception:
        return comps


async def handle_pricing_pick(interaction, si):
    """A member picked a service in /pricing — post that service's pricing
    PUBLICLY in the channel (designed layout if set, else a simple embed)."""
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        await _raw_interaction_reply(interaction, 7, content="Pick a service.",
                                     components=[_pricing_service_select("pricing_svc")])
        return
    name = services[si].get("name") or ""
    channel = interaction.channel
    guild = interaction.guild
    posted = False
    try:
        comps = _render_pricing_components(si, guild)
        if comps and channel:
            # Render designer @mentions as names WITHOUT pinging everyone listed.
            posted = bool(await send_v2_message(channel, comps, allowed_mentions={"parse": []}))
        elif channel:
            e = _pricing_embed(si, guild)
            if e:
                await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                posted = True
    except Exception as ex:
        print(f"[Pricing] post failed: {ex}")
    content = (f"Posted **{name}** pricing below. Pick another to post it too."
               if posted else "Couldn't post the pricing — please try again.")
    await _raw_interaction_reply(interaction, 7, content=content,
                                 components=[_pricing_service_select("pricing_svc")])


async def _ensure_pricing_loaded():
    """Make /pricing and /setpricing self-healing. If the structure wasn't picked
    up on boot, load it; either way ALWAYS refresh the prices from the database so
    a redeploy (or a boot-time hiccup) can never show stale/blank pricing."""
    try:
        if not pricing_config.get("services"):
            cfg = await fetch_config("customs-pricing")
            if cfg:
                await apply_config("customs-pricing", cfg)
                print(f"[Pricing] lazy-loaded — {len(pricing_config.get('services') or [])} services")
                return  # apply_config already refreshed values from the DB
        # Structure present — just pull the latest prices from the DB.
        res = await _pricing_call("get")
        if isinstance(res, dict) and res.get("ok"):
            pricing_config["values"] = res.get("prices") or {}
    except Exception as e:
        print(f"[Pricing] refresh failed: {e}")


async def _edit_original_select(interaction, prompt, custom_id):
    """Edit the deferred ephemeral response into a service-select prompt."""
    route = discord.http.Route(
        "PATCH", "/webhooks/{application_id}/{interaction_token}/messages/@original",
        application_id=bot.application_id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={
        "content": prompt,
        "components": [_pricing_service_select(custom_id)],
    })


@bot.tree.command(name="pricing", description="View pricing for a service")
async def pricing_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    await _ensure_pricing_loaded()
    if not (pricing_config.get("services") or []):
        await interaction.followup.send(embed=info_embed("No pricing", "Pricing isn't set up yet."), ephemeral=True)
        return
    try:
        await _edit_original_select(interaction, "Pick a service to see its pricing:", "pricing_svc")
    except Exception as e:
        print(f"[Pricing] /pricing failed: {e}")


@bot.tree.command(name="setpricing", description="Set prices for a service (designers only)")
async def setpricing_cmd(interaction: discord.Interaction):
    if not _pricing_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only designers can set pricing."), ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    await _ensure_pricing_loaded()
    if not (pricing_config.get("services") or []):
        await interaction.followup.send(embed=info_embed("No services", "Add services in the dashboard Pricing block first, then Save."), ephemeral=True)
        return
    try:
        await _edit_original_select(interaction, "Pick a service to edit its prices:", "setprice_svc")
    except Exception as e:
        print(f"[Pricing] /setpricing failed: {e}")


async def _open_setprice(interaction, si):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if not items:
        await _raw_interaction_reply(interaction, 7, content=f"**{name}** has no items to price.", components=[])
        return
    # Each item takes a Robux + USD field, so always pick the item first, then
    # a 2-field modal for that item.
    options = [{"label": it[:100], "value": str(i)} for i, it in enumerate(items[:25])]
    row = {"type": 1, "components": [{"type": 3, "custom_id": f"setprice_item:{si}",
                                      "placeholder": "Pick an item to price", "options": options}]}
    await _raw_interaction_reply(interaction, 7, content=f"**{name}** — pick an item:", components=[row])


async def _open_setprice_one(interaction, si, ii):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if ii < 0 or ii >= len(items):
        return
    item = items[ii]
    uid = str(interaction.user.id)
    mine = ((pricing_config.get("values") or {}).get(name, {}) or {}).get(uid, {})
    robux, usd = _price_parts(mine.get(item))
    components = [
        {"type": 18, "label": f"{item[:30]} — Robux",
         "component": {"type": 4, "custom_id": "robux", "style": 1, "required": False,
                       "max_length": 20, "value": robux, "placeholder": "e.g. 1500 (blank = none)"}},
        {"type": 18, "label": f"{item[:30]} — USD",
         "component": {"type": 4, "custom_id": "usd", "style": 1, "required": False,
                       "max_length": 20, "value": usd, "placeholder": "e.g. 15 (blank = none)"}},
    ]
    data = {"title": f"{item} price"[:45], "custom_id": f"setprice_one:{si}:{ii}", "components": components}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 9, "data": data})


async def _save_pricing_entries(entries):
    """Persist price entries and update the in-memory cache."""
    res = await _pricing_call("set", entries)
    if isinstance(res, dict) and res.get("ok"):
        pricing_config["values"] = res.get("prices") or pricing_config.get("values") or {}
        return True, None
    return False, (res or {}).get("error", "Unknown error")


async def handle_setprice_one_submit(interaction, si, ii):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if ii < 0 or ii >= len(items):
        return
    item = items[ii]
    vals = _modal_values((interaction.data or {}).get("components"))
    robux = str(vals.get("robux") or "").strip()
    usd = str(vals.get("usd") or "").strip()
    # Saved under the DESIGNER who ran /setpricing — each person has their own.
    ok, err = await _save_pricing_entries([{
        "service": name, "user": str(interaction.user.id), "item": item, "robux": robux, "usd": usd,
    }])
    if not ok:
        await interaction.followup.send(embed=error_embed("Couldn't save", str(err)[:400]), ephemeral=True)
        return
    await interaction.followup.send(embed=_pricing_embed(si, interaction.guild), ephemeral=True)


# ===================== Join Setup =====================
# /joinsetup onboards a designer: set your prices (same picker as /setpricing),
# then click Done to auto-create your portfolio post. It reuses the pricing
# save logic and the shared _do_portfolio_post helper.

def _joinsetup_service_rows():
    """The service picker plus a persistent Done button, shown at each step."""
    services = pricing_config.get("services") or []
    options = [{"label": s["name"][:100], "value": str(i)} for i, s in enumerate(services[:25])]
    return [
        {"type": 1, "components": [{"type": 3, "custom_id": "joinsetup_svc",
                                    "placeholder": "Pick a service to set your prices", "options": options}]},
        {"type": 1, "components": [{"type": 2, "style": 3, "custom_id": "joinsetup_done",
                                    "label": "Done — Create my portfolio"}]},
    ]


@bot.tree.command(name="joinsetup", description="Set your pricing, then get your portfolio")
async def joinsetup_cmd(interaction: discord.Interaction):
    if not _pricing_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only designers can run setup."), ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    await _ensure_pricing_loaded()
    if not (pricing_config.get("services") or []):
        await interaction.followup.send(embed=info_embed("No services", "Add services in the dashboard Pricing block first, then Save."), ephemeral=True)
        return
    route = discord.http.Route(
        "PATCH", "/webhooks/{application_id}/{interaction_token}/messages/@original",
        application_id=bot.application_id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={
        "content": "**Step 1 — Set your prices.** Pick each service and fill in your Robux/USD. "
                   "When you're finished, click **Done — Create my portfolio**.",
        "components": _joinsetup_service_rows(),
    })


async def _joinsetup_open_item(interaction, si):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if not items:
        await _raw_interaction_reply(interaction, 7, content=f"**{name}** has no items to price. Pick another, or click Done:", components=_joinsetup_service_rows())
        return
    options = [{"label": it[:100], "value": str(i)} for i, it in enumerate(items[:25])]
    rows = [
        {"type": 1, "components": [{"type": 3, "custom_id": f"joinsetup_item:{si}",
                                    "placeholder": "Pick an item to price", "options": options}]},
        {"type": 1, "components": [{"type": 2, "style": 3, "custom_id": "joinsetup_done",
                                    "label": "Done — Create my portfolio"}]},
    ]
    await _raw_interaction_reply(interaction, 7, content=f"**{name}** — pick an item to price (or click Done when finished):", components=rows)


async def _joinsetup_open_one(interaction, si, ii):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if ii < 0 or ii >= len(items):
        return
    item = items[ii]
    uid = str(interaction.user.id)
    mine = ((pricing_config.get("values") or {}).get(name, {}) or {}).get(uid, {})
    robux, usd = _price_parts(mine.get(item))
    components = [
        {"type": 18, "label": f"{item[:30]} — Robux",
         "component": {"type": 4, "custom_id": "robux", "style": 1, "required": False,
                       "max_length": 20, "value": robux, "placeholder": "e.g. 1500 (blank = none)"}},
        {"type": 18, "label": f"{item[:30]} — USD",
         "component": {"type": 4, "custom_id": "usd", "style": 1, "required": False,
                       "max_length": 20, "value": usd, "placeholder": "e.g. 15 (blank = none)"}},
    ]
    data = {"title": f"{item} price"[:45], "custom_id": f"joinsetup_one:{si}:{ii}", "components": components}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 9, "data": data})


async def _joinsetup_one_submit(interaction, si, ii):
    services = pricing_config.get("services") or []
    if si < 0 or si >= len(services):
        return
    svc = services[si]
    name = svc.get("name") or ""
    items = svc.get("items") or []
    if ii < 0 or ii >= len(items):
        return
    item = items[ii]
    vals = _modal_values((interaction.data or {}).get("components"))
    robux = str(vals.get("robux") or "").strip()
    usd = str(vals.get("usd") or "").strip()
    ok, err = await _save_pricing_entries([{
        "service": name, "user": str(interaction.user.id), "item": item, "robux": robux, "usd": usd,
    }])
    if ok:
        note = f"Saved **{item}** ✓ — set more prices, or click **Done — Create my portfolio**."
    else:
        note = f"Couldn't save **{item}**: {str(err)[:200]}"
    # Update the message (the modal was launched from a component) back to the picker.
    await _raw_interaction_reply(interaction, 7, content=note, components=_joinsetup_service_rows())


async def _joinsetup_done(interaction):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    await _do_portfolio_post(interaction)


def _parse_gw_cid(raw):
    """Split the Enter button's custom_id payload ('gid' or 'gid|end_ts|winners')."""
    parts = str(raw).split("|")
    gid = parts[0]
    end_ts = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    winners = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return gid, end_ts, winners


async def _gw_entries_call(action, gid=None, uid=None, meta=None, entrants=None):
    """Persist/read giveaway state server-side so giveaways + entries survive
    redeploys. Supports get_all (no gid), get, set_state, add, remove, clear."""
    payload = {"action": action}
    if gid is not None:
        payload["gid"] = str(gid)
    if uid is not None:
        payload["uid"] = str(uid)
    if meta is not None:
        payload["meta"] = meta
    if entrants is not None:
        payload["entrants"] = [str(u) for u in entrants]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/giveaway-entries",
                headers=_fn_headers(), json=payload, timeout=15,
            )
            data = r.json() if r.content else {}
            if r.status_code != 200 or (isinstance(data, dict) and data.get("error")):
                # A 404 here means the 'giveaway-entries' edge function isn't deployed.
                print(f"[Giveaway] entries {action} -> HTTP {r.status_code}: {str(data)[:200]}")
            return data
    except Exception as e:
        print(f"[Giveaway] entries {action} call failed: {e}")
        return {"error": str(e)[:200]}


def _gw_meta(g):
    """JSON-safe snapshot of a giveaway's metadata (everything but the entrants set)."""
    return {k: v for k, v in g.items() if k != "entrants"}


async def _gw_save_state(gid, g):
    """Persist a giveaway's full state (metadata + entrants). Called on create,
    on end, and on shutdown so the whole giveaway is remembered across redeploys."""
    try:
        await _gw_entries_call("set_state", gid=gid, meta=_gw_meta(g),
                               entrants=list(g.get("entrants") or []))
    except Exception as e:
        print(f"[Giveaway] save_state {gid} failed: {e}")


async def _gw_restore_all():
    """On boot, rebuild EVERY saved giveaway into memory with its entrants and
    re-arm its timer — so giveaways come back fully after a redeploy without
    waiting for anyone to click."""
    res = await _gw_entries_call("get_all")
    if not (isinstance(res, dict) and res.get("ok")):
        print(f"[Giveaway] restore skipped (is 'giveaway-entries' deployed?): {(res or {}).get('error')}")
        return
    gws = res.get("giveaways") or {}
    now = int(time.time())
    restored = 0
    for gid, state in gws.items():
        meta = (state or {}).get("meta") or {}
        if not meta.get("channel_id"):
            continue  # incomplete (legacy entrants-only) — handled lazily on click
        g = dict(meta)
        g["entrants"] = set(str(u) for u in ((state or {}).get("entrants") or []))
        g.setdefault("ended", False)
        g.setdefault("winners", 1)
        active_giveaways[gid] = g
        restored += 1
        if not g.get("ended"):
            end_ts = int(g.get("end_ts") or 0)
            if end_ts:
                remaining = end_ts - now
                if remaining <= 0:
                    asyncio.create_task(end_giveaway(gid))
                else:
                    asyncio.create_task(_giveaway_timer(gid, remaining))
                    # Re-draw the message so the Entries count reflects the restored
                    # entrants immediately (not the pre-redeploy render).
                    asyncio.create_task(_giveaway_refresh_count(gid))
    if restored:
        print(f"[Giveaway] restored {restored} giveaway(s) from storage")


async def _giveaway_adopt(interaction, gid, end_ts, winners):
    """Rebuild a running giveaway the process lost on restart, from the button's
    encoded end time/winners + the message it's on, then reschedule its end.
    Entrants are reloaded from storage so nobody's entry is ever dropped."""
    if end_ts is None:
        return None
    msg = getattr(interaction, "message", None)
    g = {
        "message_id": str(msg.id) if msg else None,
        "channel_id": str(interaction.channel.id),
        "guild_id": str(getattr(interaction.guild, "id", "") or ""),
        "prize": "", "winners": max(1, winners or 1), "end_ts": int(end_ts),
        "length": "", "host_id": "", "entrants": set(), "ended": False, "design": None,
    }
    active_giveaways[gid] = g
    # Restore the persisted entrant list from before the restart.
    try:
        res = await _gw_entries_call("get", gid)
        if isinstance(res, dict) and res.get("ok"):
            g["entrants"] = set(str(u) for u in (res.get("entrants") or []))
    except Exception as e:
        print(f"[Giveaway] entrant restore failed for {gid}: {e}")
    remaining = int(end_ts) - int(time.time())
    asyncio.create_task(end_giveaway(gid) if remaining <= 0 else _giveaway_timer(gid, remaining))
    print(f"[Giveaway] re-adopted {gid} after restart ({len(g['entrants'])} entrants, ends in {remaining}s)")
    return g


async def giveaway_enter(interaction, raw):
    gid, end_ts, winners = _parse_gw_cid(raw)
    g = active_giveaways.get(gid)
    if not g:
        # Lost on restart — rebuild from the button so it never breaks.
        g = await _giveaway_adopt(interaction, gid, end_ts, winners)
    if not g:
        await interaction.response.send_message(embed=error_embed("Giveaway unavailable", "This giveaway is no longer active."), ephemeral=True)
        return
    if g.get("ended"):
        await interaction.response.send_message(embed=error_embed("Giveaway ended", "This giveaway has already ended."), ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid in g["entrants"]:
        g["entrants"].discard(uid)
        msg, entry_action = "Giveaway Left", "remove"
    else:
        g["entrants"].add(uid)
        msg, entry_action = "Giveaway Entered", "add"
    await interaction.response.send_message(msg, ephemeral=True)
    # Persist the entry so it survives a redeploy, then refresh the live count.
    await _gw_entries_call(entry_action, gid, uid)
    await _giveaway_refresh_count(gid)


async def _cmd_reroll(message):
    """'-reroll' — draw a new winner for an ended giveaway. Only giveaway managers
    can use it. Reply to a specific giveaway to reroll that one; otherwise it picks
    the most recently ended giveaway in the channel. Edits the giveaway message in
    place (no new message) and reacts to confirm."""
    async def react(emoji):
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass

    if not _giveaway_can_manage(message.author):
        return await react("⛔")

    chan_id = str(message.channel.id)
    ref = getattr(message, "reference", None)
    ref_mid = str(ref.message_id) if ref and getattr(ref, "message_id", None) else None

    target = None
    if ref_mid:
        for gid, g in active_giveaways.items():
            if str(g.get("channel_id")) == chan_id and str(g.get("message_id")) == ref_mid:
                target = (gid, g)
                break
    if target is None:
        ended = [(gid, g) for gid, g in active_giveaways.items()
                 if str(g.get("channel_id")) == chan_id and g.get("ended")]
        if ended:
            target = max(ended, key=lambda kv: int(kv[1].get("end_ts") or 0))

    if target is None or not target[1].get("ended"):
        return await react("❓")

    gid, g = target
    winners = _pick_winners(g["entrants"], g["winners"])
    if not winners:
        return await react("❌")

    g["last_winners"] = winners
    guild = message.guild
    await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=True, winner_ids=winners, for_edit=True))
    await react("✅")


@bot.event
async def on_raw_message_delete(payload):
    """The giveaway message lives forever on its own — the bot only ever edits it.
    The one thing that ends a giveaway's tracking is the message being MANUALLY
    deleted: when that happens, drop the giveaway so nothing tries to edit a gone
    message and its end timer becomes a no-op."""
    mid = str(getattr(payload, "message_id", ""))
    if not mid:
        return
    for gid, g in list(active_giveaways.items()):
        if str(g.get("message_id")) == mid:
            active_giveaways.pop(gid, None)
            print(f"[Giveaway] {gid} message {mid} was deleted — dropped from tracking")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Form submits arrive as modal_submit interactions (not component). Handle
    # ours here; leave every other modal (Close Order, etc.) to discord.py's own
    # Modal dispatch by returning. This fires regardless of restarts, so forms
    # keep working across redeploys.
    if interaction.type == discord.InteractionType.modal_submit:
        cid = (interaction.data or {}).get("custom_id", "")
        if cid.startswith("ticketform:"):
            payload = cid.split(":", 1)[1]
            if "|" in payload:
                fkey, pg = payload.rsplit("|", 1)
                try:
                    pg = int(pg)
                except Exception:
                    pg = 0
            else:
                fkey, pg = payload, 0
            await handle_ticket_form_submit(interaction, fkey, pg)
        elif cid == "giveawayform":
            await handle_giveaway_form_submit(interaction)
        elif cid.startswith("robuxstockform:"):
            try:
                funds = int(cid.split(":", 1)[1])
            except Exception:
                funds = 0
            await handle_robux_stock_submit(interaction, funds)
        elif cid == "robuxbuyform":
            await handle_robux_buy_submit(interaction)
        elif cid.startswith("setprice_one:"):
            parts = cid.split(":")
            try:
                si, ii = int(parts[1]), int(parts[2])
            except Exception:
                si, ii = -1, -1
            await handle_setprice_one_submit(interaction, si, ii)
        elif cid.startswith("joinsetup_one:"):
            parts = cid.split(":")
            try:
                si, ii = int(parts[1]), int(parts[2])
            except Exception:
                si, ii = -1, -1
            await _joinsetup_one_submit(interaction, si, ii)
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
                if await _orderlog_gate_block(interaction, mk):
                    return
                await open_ticket(interaction, v, open_comps_override=ticket_msgs.get(mk), category_name_override=ticket_categories.get(mk), access_names_override=ticket_access.get(mk))
            elif v.startswith("ticket_form:"):
                fk = v.split(":", 1)[1]
                if await _orderlog_gate_block(interaction, fk):
                    return
                await open_ticket_form(interaction, fk)
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
        if await _orderlog_gate_block(interaction, mk):
            return
        await open_ticket(interaction, cid, open_comps_override=ticket_msgs.get(mk), category_name_override=ticket_categories.get(mk), access_names_override=ticket_access.get(mk))
    elif cid.startswith("ticket_form:"):
        fk = cid.split(":", 1)[1]
        if await _orderlog_gate_block(interaction, fk):
            return
        await open_ticket_form(interaction, fk)
    elif cid.startswith("formcont:"):
        payload = cid.split(":", 1)[1]
        fkey, pg = (payload.rsplit("|", 1) + ["0"])[:2] if "|" in payload else (payload, "0")
        try:
            pg = int(pg)
        except Exception:
            pg = 0
        await _open_form_page(interaction, fkey, pg)
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
    elif cid.startswith("gw:"):
        await giveaway_enter(interaction, cid.split(":", 1)[1])
    elif cid.startswith("robuxstock:"):
        try:
            funds = int(cid.split(":", 1)[1])
        except Exception:
            funds = 0
        await _open_robux_stock_modal(interaction, funds)
    elif cid == "robuxbuy":
        await handle_robux_buy_click(interaction)
    elif cid.startswith("notifyrole:"):
        await handle_notify_click(interaction, cid.split(":", 1)[1])
    elif cid == "orderstatus":
        await show_order_status(interaction)
    elif cid == "pricing_svc":
        vals = (interaction.data or {}).get("values") or []
        try:
            si = int(vals[0]) if vals else -1
        except Exception:
            si = -1
        await handle_pricing_pick(interaction, si)
    elif cid == "setprice_svc":
        vals = (interaction.data or {}).get("values") or []
        try:
            si = int(vals[0]) if vals else -1
        except Exception:
            si = -1
        await _open_setprice(interaction, si)
    elif cid.startswith("setprice_item:"):
        vals = (interaction.data or {}).get("values") or []
        try:
            si = int(cid.split(":", 1)[1])
            ii = int(vals[0]) if vals else -1
        except Exception:
            si, ii = -1, -1
        await _open_setprice_one(interaction, si, ii)
    elif cid == "joinsetup_svc":
        vals = (interaction.data or {}).get("values") or []
        try:
            si = int(vals[0]) if vals else -1
        except Exception:
            si = -1
        await _joinsetup_open_item(interaction, si)
    elif cid.startswith("joinsetup_item:"):
        vals = (interaction.data or {}).get("values") or []
        try:
            si = int(cid.split(":", 1)[1])
            ii = int(vals[0]) if vals else -1
        except Exception:
            si, ii = -1, -1
        await _joinsetup_open_one(interaction, si, ii)
    elif cid == "joinsetup_done":
        await _joinsetup_done(interaction)


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


# How many open tickets a member may have per section (category) at once.
MAX_TICKETS_PER_SECTION = 2


def _user_ticket_count_for(guild, user_id, cat_name, fallback_cat_channel):
    """Count a member's open tickets in one section.
    - cat_name given → match channels whose Discord category name == cat_name
      (so all Ticket/Form types that share a category name count together).
    - no cat_name → match channels in the fallback (global) category / uncategorized.
    """
    uid = str(user_id)
    target_name = (cat_name or "").strip().lower()
    fb_id = fallback_cat_channel.id if fallback_cat_channel else None
    count = 0
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if not (topic.startswith("ticket|") and topic.split("|")[1] == uid):
            continue
        if target_name:
            ch_name = ch.category.name.strip().lower() if ch.category else ""
            if ch_name == target_name:
                count += 1
        else:
            ch_id = ch.category.id if ch.category else None
            if ch_id == fb_id:
                count += 1
    return count


def _open_ticket_count_for_category(guild, cat_name):
    """Count ALL open order tickets in one Discord category (any opener). Used by
    the Order Status embed to decide open/limited/closed per service."""
    target = (cat_name or "").strip().lower()
    if not (guild and target):
        return 0
    count = 0
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if not topic.startswith("ticket|"):
            continue
        ch_cat = ch.category.name.strip().lower() if ch.category else ""
        if ch_cat == target:
            count += 1
    return count


def _clean_label(s):
    """Strip markdown emphasis so a {Question: **Server Name:**} token shows a
    clean 'Server Name:' label in the modal instead of literal asterisks."""
    return re.sub(r"[*_`~]", "", s or "").strip()


def _parse_questions(open_comps, limit=5):
    """Ordered, de-duplicated list of {Question: LABEL} labels in a design.
    Discord modals hold 5 fields each; ticket forms page across two modals so
    they allow up to 10 (limit=10). Other callers keep the single-modal 5."""
    raw = json.dumps(open_comps or [])
    seen = []
    for m in _QUESTION_RE.finditer(raw):
        lbl = (m.group(1) or "").strip()
        if lbl and lbl not in seen:
            seen.append(lbl)
    return seen[:limit]


# In-progress ticket-form answers between paged modals, keyed by (user_id, key).
_pending_form_answers = {}
FORM_PAGE_SIZE = 5
FORM_MAX_QUESTIONS = 10


async def _open_form_page(interaction, key, page):
    """Open the modal for one page (5 questions) of a ticket form. Called as the
    response to the Form button (page 0) or a 'Continue' button (later pages)."""
    open_comps = form_msgs.get(key) or []
    questions = _parse_questions(open_comps, limit=FORM_MAX_QUESTIONS)
    start = page * FORM_PAGE_SIZE
    page_qs = questions[start:start + FORM_PAGE_SIZE]
    if not page_qs:
        return
    total_pages = (len(questions) + FORM_PAGE_SIZE - 1) // FORM_PAGE_SIZE
    components = []
    for j, q in enumerate(page_qs):
        idx = start + j
        components.append({
            "type": 18,  # Label — carries the field label
            "label": (_clean_label(q) or q)[:45],
            "component": {
                "type": 4, "custom_id": f"q{idx}", "style": _form_input_style(q),
                "required": True, "max_length": 1000,
            },
        })
    title = (form_titles.get(key) or "Application")
    if total_pages > 1:
        title = f"{title} ({page + 1}/{total_pages})"
    data = {"title": title[:45], "custom_id": f"ticketform:{key}|{page}", "components": components}
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={"type": 9, "data": data})


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
    questions = _parse_questions(open_comps, limit=FORM_MAX_QUESTIONS)
    if not questions:
        # No questions defined — behave exactly like a Ticket button.
        await open_ticket(interaction, f"ticket_form:{key}", open_comps_override=open_comps)
        return

    guild = interaction.guild
    if guild and ticket_config.get("one_per_user", True):
        cat_name = ticket_categories.get(key)
        fb = None
        if not cat_name:
            cid = ticket_config.get("category_id") or ""
            if cid:
                fb = guild.get_channel(int(cid))
        if _user_ticket_count_for(guild, interaction.user.id, cat_name, fb) >= MAX_TICKETS_PER_SECTION:
            try:
                await interaction.response.send_message(
                    embed=error_embed("Limit reached", f"You already have {MAX_TICKETS_PER_SECTION} open tickets in this section. Please close one before opening another."),
                    ephemeral=True,
                )
            except Exception:
                pass
            return

    # Start fresh, then open page 1 of the form (up to 5 questions per page,
    # continued with a button if there are more — Discord caps a modal at 5).
    _pending_form_answers.pop((interaction.user.id, key), None)
    try:
        await _open_form_page(interaction, key, 0)
    except Exception as e:
        print(f"[Ticket] form modal failed: {e}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", "Please try again."), ephemeral=True)
        except Exception:
            pass


async def handle_ticket_form_submit(interaction, key, page=0):
    open_comps = form_msgs.get(key) or []
    questions = _parse_questions(open_comps, limit=FORM_MAX_QUESTIONS)
    total_pages = (len(questions) + FORM_PAGE_SIZE - 1) // FORM_PAGE_SIZE

    # Stash this page's answers (keyed to the member so pages accumulate).
    vals = _collect_modal_values((interaction.data or {}).get("components"))
    pend = _pending_form_answers.setdefault((interaction.user.id, key), {})
    start = page * FORM_PAGE_SIZE
    for j, q in enumerate(questions[start:start + FORM_PAGE_SIZE]):
        pend[q] = (vals.get(f"q{start + j}") or "").strip()

    # More questions to go — offer a Continue button that opens the next modal
    # (button -> modal is always allowed, unlike modal -> modal).
    if page + 1 < total_pages:
        remaining = len(questions) - (page + 1) * FORM_PAGE_SIZE
        row = {"type": 1, "components": [{
            "type": 2, "style": 1, "custom_id": f"formcont:{key}|{page + 1}", "label": "Continue",
        }]}
        data = {"flags": 1 << 6,
                "content": f"Saved — **{remaining}** more question{'s' if remaining != 1 else ''} to go. Tap **Continue**.",
                "components": [row]}
        try:
            route = discord.http.Route(
                "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id, interaction_token=interaction.token)
            await bot.http.request(route, json={"type": 4, "data": data})
        except Exception as e:
            print(f"[Ticket] form continue prompt failed: {e}")
        return

    # Last page — acknowledge, then build the ticket with ALL collected answers.
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception as e:
        print(f"[Ticket] form submit defer failed: {e}")
    try:
        mapping = dict(_pending_form_answers.pop((interaction.user.id, key), {}))
        substituted = _apply_answers(open_comps, mapping)
        await open_ticket(interaction, f"ticket_form:{key}", open_comps_override=substituted,
                          category_name_override=ticket_categories.get(key), access_names_override=ticket_access.get(key), already_responded=True)
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


def _resolve_role_names(guild, names_csv):
    """Turn a comma-separated list of role names into role objects (case-insensitive)."""
    if not names_csv or not guild:
        return []
    wanted = [n.strip().lower() for n in str(names_csv).split(",") if n.strip()]
    if not wanted:
        return []
    out = []
    for role in guild.roles:
        if role.is_default():
            continue
        if role.name.strip().lower() in wanted and role not in out:
            out.append(role)
    return out


async def open_ticket(interaction, category, open_comps_override=None, category_name_override=None, access_names_override=None, already_responded=False):
    guild = interaction.guild
    if not guild:
        return
    if not already_responded:
        await interaction.response.defer(ephemeral=True)

    # Per-Ticket/Form category (by name, created on demand) wins; otherwise fall
    # back to the globally configured category id.
    category_channel = None
    if category_name_override:
        category_channel = await _get_or_create_category(guild, category_name_override)
    if category_channel is None:
        cat_id = ticket_config.get("category_id") or ""
        if cat_id:
            category_channel = guild.get_channel(int(cat_id))

    # Limit: up to MAX_TICKETS_PER_SECTION open tickets per section (category).
    if ticket_config.get("one_per_user", True):
        open_count = _user_ticket_count_for(guild, interaction.user.id, category_name_override, category_channel)
        if open_count >= MAX_TICKETS_PER_SECTION:
            await interaction.followup.send(embed=error_embed("Limit reached", f"You already have {MAX_TICKETS_PER_SECTION} open tickets in this section. Please close one before opening another."), ephemeral=True)
            return

    support_roles = []
    for rid in ticket_config.get("support_role_ids", []):
        role = guild.get_role(int(rid))
        if role:
            support_roles.append(role)
    # Per-Ticket/Form access roles (by name) — who can SEE this ticket. Kept
    # separate from support_roles so they grant visibility without being pinged.
    access_roles = _resolve_role_names(guild, access_names_override)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    for role in support_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    for role in access_roles:
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

    # No auto-pings. Support roles get channel access (above) but are never
    # pinged automatically — the ticket shows ONLY the designed message. To ping
    # someone, write @role directly into the ticket/form design.
    content = None

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
    print(f"[Tickets] show_ephemeral key={key!r} registered={key in eph_msgs} len={len(comps) if comps else 0} "
          f"all_eph={{{', '.join(f'{k}:{len(v or [])}' for k, v in eph_msgs.items())}}}")
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
    is_opener = str(interaction.user.id) == opener_id
    if not (_is_ticket_staff(interaction.user, channel) or is_opener):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff or the opener can close this."), ephemeral=True)
        return
    await interaction.response.send_message(embed=info_embed("Closing order", "Saving transcript and closing\u2026"))
    await _do_close(channel, interaction.guild, interaction.user)


def _is_ticket_staff(member, channel=None):
    try:
        if member.guild_permissions.manage_channels:
            return True
    except Exception:
        pass
    # Global support roles (see & manage ALL tickets), if any are configured.
    if has_any_role(member, ticket_config.get("support_role_ids", [])):
        return True
    # Per-ticket: any role granted view access to THIS channel is staff for it,
    # so a section's Access roles can claim/close their own tickets.
    if channel is not None:
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        try:
            for target, ow in channel.overwrites.items():
                if isinstance(target, discord.Role) and not target.is_default() and ow.view_channel and target.id in member_role_ids:
                    return True
        except Exception:
            pass
    return False


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


def _has_claim_button(components):
    for c in (components or []):
        if not isinstance(c, dict):
            continue
        if c.get("type") == 2 and c.get("custom_id") in ("ticket_claim", "ticket_unclaim"):
            return True
        if isinstance(c.get("components"), list) and _has_claim_button(c["components"]):
            return True
    return False


async def _find_claim_message(channel):
    """Find the ticket's opening message (the one carrying the Claim button) so a
    -claim/-unclaim text command can toggle it just like the button does."""
    try:
        async for m in channel.history(limit=15, oldest_first=True):
            if not (m.author and m.author.id == bot.user.id):
                continue
            try:
                raw = await bot.http.get_message(channel.id, m.id)
            except Exception:
                continue
            if _has_claim_button(raw.get("components", [])):
                return m
    except Exception:
        pass
    return None


async def ticket_claim_toggle(interaction, claimed):
    member = interaction.user
    if not _is_ticket_staff(member, interaction.channel):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can claim orders."), ephemeral=True)
        return
    channel, msg = interaction.channel, interaction.message
    if claimed:
        await interaction.response.send_message(embed=info_embed("Order claimed", f"{member.mention} claimed this order."))
    else:
        await interaction.response.send_message(embed=info_embed("Order unclaimed", f"{member.mention} unclaimed this order."))
    await _do_claim_toggle(channel, member, claimed, msg)


async def _do_claim_toggle(channel, member, claimed, msg):
    # Toggle the Claim/Unclaim button on the ticket message (if we have it).
    if msg is not None:
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


# ---- Text commands: -claim / -unclaim / -close (mirror the buttons) ----
async def _cmd_claim(message, claimed):
    channel = message.channel
    member = message.author
    if not _is_ticket_staff(member, channel):
        await channel.send(embed=error_embed("No permission", "Only staff can claim orders."), delete_after=10)
        return
    msg = await _find_claim_message(channel)
    verb = "claimed" if claimed else "unclaimed"
    await channel.send(embed=info_embed(f"Order {verb}", f"{member.mention} {verb} this order."))
    await _do_claim_toggle(channel, member, claimed, msg)


async def _cmd_close(message, reason=""):
    channel = message.channel
    topic = getattr(channel, "topic", "") or ""
    opener_id = topic.split("|")[1] if len(topic.split("|")) > 1 else ""
    member = message.author
    if not (_is_ticket_staff(member, channel) or str(member.id) == opener_id):
        await channel.send(embed=error_embed("No permission", "Only staff or the opener can close this."), delete_after=10)
        return
    await channel.send(embed=info_embed("Closing order", "Saving transcript and closing…"))
    await _do_close(channel, message.guild, member, (reason or "").strip())


@bot.event
async def on_message(message):
    # Ticket text commands work only inside a ticket channel; everything else
    # falls through to the normal command processor.
    if not message.author.bot and message.guild:
        parts = (message.content or "").strip().split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        if cmd == "-reroll":
            await _cmd_reroll(message)
            return
        if cmd in ("-claim", "-unclaim", "-close"):
            topic = getattr(message.channel, "topic", "") or ""
            if topic.startswith("ticket|"):
                if cmd == "-close":
                    await _cmd_close(message, parts[1] if len(parts) > 1 else "")
                else:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await _cmd_claim(message, cmd == "-claim")
                return
            else:
                await message.channel.send(embed=error_embed("Not a ticket", "This command only works inside a ticket channel."), delete_after=10)
                return
    await bot.process_commands(message)


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


def _build_v2(comp, guild):
    """Convert one dashboard V2 item into a raw Discord Components-V2 object.
    Module-level so both send_v2_message and the giveaway renderer can use it."""
    ctype = comp.get("type", "")
    if ctype in ("text", "text_display"):
        text = comp.get("text") or comp.get("content", "")
        title = comp.get("title", "")
        if title:
            text = f"**{title}**\n{text}" if text else f"**{title}**"
        return {"type": 10, "content": _render_guild_text(text, guild)} if text else None
    if ctype == "container":
        accent = comp.get("accentColor") or comp.get("accent_color", "")
        try:
            accent_int = int(str(accent).lstrip("#"), 16) if accent else None
        except Exception:
            accent_int = None
        children = [_build_v2(c, guild) for c in comp.get("children", [])]
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
        text = _render_guild_text(text, guild)
        thumb = comp.get("thumbnailUrl") or comp.get("thumbnail_url")
        button = comp.get("button")
        accessory = None
        if thumb and str(thumb).startswith("http"):
            accessory = {"type": 11, "media": {"url": thumb}}
        elif isinstance(button, dict) and button.get("label"):
            accessory = build_button(button, guild)
        # A Components V2 Section (type 9) REQUIRES an accessory (thumbnail or
        # button). If the design has neither, Discord rejects the whole
        # message, so render the text as a plain text display instead.
        if accessory is None:
            return {"type": 10, "content": text}
        return {"type": 9, "components": [{"type": 10, "content": text}], "accessory": accessory}
    if ctype in ("buttonRow", "button_row", "buttons", "action_row"):
        buttons = [build_button(b, guild) for b in comp.get("buttons", [])]
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


async def send_v2_message(channel, components_v2, content=None, interaction=None, ephemeral=False, allowed_mentions=None):
    _guild = getattr(channel, "guild", None)

    built = [b for b in (_build_v2(c, _guild) for c in components_v2) if b]
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


def _v2_thread_name(components_v2, default="Portfolio"):
    """Derive a forum-post title from the first bit of text in the design."""
    def _first_text(items):
        for c in items or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("text", "text_display"):
                t = (c.get("title") or c.get("text") or c.get("content") or "").strip()
                if t:
                    return t
            for kids in (c.get("children"), c.get("components")):
                if isinstance(kids, list):
                    t = _first_text(kids)
                    if t:
                        return t
        return ""
    raw = _first_text(components_v2) or default
    # First line only, strip markdown emphasis, cap at Discord's 100-char limit.
    line = raw.splitlines()[0].replace("*", "").replace("_", "").replace("#", "").strip()
    return (line or default)[:100]


async def send_v2_forum_post(forum, components_v2, name=None):
    """Create a forum post (thread) carrying a Components-V2 message. Forum
    channels can't take a plain message — a thread with a starter message is
    the only way to post into them. Returns the thread id (truthy) or False."""
    _guild = getattr(forum, "guild", None)
    built = [b for b in (_build_v2(c, _guild) for c in components_v2) if b]
    if not built:
        return False
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    if not {c.get("type") for c in built}.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    payload = {
        "name": name or _v2_thread_name(components_v2),
        "message": {"components": built, "flags": 1 << 15},
    }
    route = discord.http.Route("POST", "/channels/{channel_id}/threads", channel_id=forum.id)
    try:
        resp = await bot.http.request(route, json=payload)
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        return True
    except discord.HTTPException as e:
        body = getattr(e, "text", "") or ""
        _V2_LAST_ERROR["msg"] = f"HTTP {getattr(e, 'status', '?')}: {body[:400]}"
        print(f"[V2] forum post failed: HTTP {getattr(e, 'status', '?')} {body[:600]}")
        return False
    except Exception as e:
        _V2_LAST_ERROR["msg"] = str(e)[:400]
        print(f"[V2] forum post failed: {e}")
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

    if btn.get("counter"):
        # Giveaway "Counter" (enter) button. The real custom_id (gw:<gid>) is
        # patched in per-giveaway by _giveaway_render_design.
        return _btn({"type": 2, "label": (label[:80] or "Enter"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": "gw:__COUNTER__"})
    if btn.get("buyrobux"):
        # Robux Locker "Buy Robux" button. Unclickable (disabled) whenever there
        # is no Available Stock — members can only buy when there's Robux there.
        out_of_stock = int(robux_locker_config.get("stock") or 0) <= 0
        return _btn({"type": 2, "label": (label[:80] or "Buy Robux"), "style": BUTTON_STYLE_MAP.get(style_name, 3), "custom_id": "robuxbuy", "disabled": out_of_stock})
    if "notify_roles" in btn:
        # Notification button — clicking toggles the selected role(s) on the
        # member. Role ids are baked into the custom_id so it survives restarts.
        role_objs = _resolve_role_names(guild, btn.get("notify_roles"))
        ids = ",".join(str(r.id) for r in role_objs)
        cid = f"notifyrole:{ids}"[:100]
        return _btn({"type": 2, "label": (label[:80] or "Notify me"), "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": cid})
    if btn.get("orderstatus"):
        # Order Status button — shows a live per-service open/limited/closed embed.
        return _btn({"type": 2, "label": (label[:80] or "Order Status"), "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": "orderstatus"})
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
        # Multi-panel: cfg.panels = [{channel_id, components}, ...]. Falls back to
        # the single panel_channel_id + panel_components for older configs. ALL
        # panels are registered so every posted panel keeps working.
        # The dashboard sends panel_channel_id = the panel currently being edited.
        # We register ALL panels (so every panel's buttons keep working) but only
        # (re)post that one on save.
        panels = _parse_ticket_panels(cfg)
        edited_ch = str(cfg.get("panel_channel_id") or (panels[0]["channel_id"] if panels else ""))
        edited_panel = next((p for p in panels if p["channel_id"] == edited_ch), (panels[0] if panels else {"components": []}))
        ticket_config["panel_channel_id"] = edited_ch
        ticket_config["panel_components"] = edited_panel.get("components", [])
        _ticket_sources["tickets"] = {"panels": panels, "types": _parse_ticket_types(cfg)}
        _rebuild_ticket_registry()
        print(f"[Config] tickets — category {ticket_config['category_id']} roles {ticket_config['support_role_ids']} panel_ch {ticket_config['panel_channel_id']} panel {len(ticket_config['panel_components'])} types {len(ticket_config['types'])}")
        # Post/refresh ONLY the panel being edited on a save (not on boot, and
        # not the other panels — those stay put).
        if post_panel:
            await post_ticket_panel(only_channel_id=edited_ch or None)
    elif feature == "credits":
        if cfg.get("manager_role_ids") is not None:
            credits_config["manager_role_ids"] = [str(x) for x in cfg["manager_role_ids"] if x]
        if cfg.get("currency_name"):
            credits_config["currency_name"] = cfg["currency_name"]
        if cfg.get("log_channel_id"):
            credits_config["log_channel_id"] = str(cfg["log_channel_id"])
        print(f"[Config] credits — managers {credits_config['manager_role_ids']}")
    elif feature in ("giveaway", "customs-giveaway"):
        if cfg.get("title") is not None:
            giveaway_config["title"] = str(cfg.get("title") or "🎉 GIVEAWAY 🎉")
        if cfg.get("color") is not None:
            try:
                giveaway_config["color"] = int(cfg["color"])
            except Exception:
                pass
        if cfg.get("button_label") is not None:
            giveaway_config["button_label"] = str(cfg.get("button_label") or "🎉 Enter")
        if cfg.get("host_line") is not None:
            giveaway_config["host_line"] = str(cfg.get("host_line") or "")
        if cfg.get("ping") is not None:
            giveaway_config["ping"] = str(cfg.get("ping") or "")
        if cfg.get("default_winners") is not None:
            try:
                giveaway_config["default_winners"] = max(1, int(cfg["default_winners"]))
            except Exception:
                pass
        if cfg.get("default_duration"):
            giveaway_config["default_duration"] = str(cfg["default_duration"])
        if cfg.get("manager_role_ids") is not None:
            giveaway_config["manager_role_ids"] = [str(x) for x in cfg["manager_role_ids"] if x]
        comps = cfg.get("components")
        giveaway_config["components"] = comps if isinstance(comps, list) else []
        ended = cfg.get("ended_components")
        giveaway_config["ended_components"] = ended if isinstance(ended, list) else []
        print(f"[Config] giveaway — managers {giveaway_config['manager_role_ids']} design {len(giveaway_config['components'])} ended {len(giveaway_config['ended_components'])}")
    elif feature in ("robux-locker", "customs-robux-locker"):
        if cfg.get("channel_id"):
            robux_locker_config["channel_id"] = str(cfg["channel_id"])
        comps = cfg.get("components")
        robux_locker_config["components"] = comps if isinstance(comps, list) else []
        # Pull the persisted Available Stock so the panel shows the right number.
        st = await _robux_locker_call("get_stock")
        if isinstance(st, dict) and st.get("ok"):
            robux_locker_config["stock"] = int(st.get("stock") or 0)
        # Pull the persisted USD-per-1k rate so Buy Robux can price the Stripe link.
        rt = await _robux_locker_call("get_rate")
        if isinstance(rt, dict) and rt.get("ok"):
            robux_locker_config["rate_per_1k"] = float(rt.get("rate_per_1k") or 0)
        print(f"[Config] robux-locker — channel {robux_locker_config['channel_id']} design {len(robux_locker_config['components'])} stock {robux_locker_config['stock']} rate ${robux_locker_config['rate_per_1k']}/1k")
        # Post/refresh the panel on a save (deliberate action), not on boot.
        if post_panel:
            await post_robux_locker_panel()
    elif feature in ("portfolio", "customs-portfolio"):
        if cfg.get("channel_id"):
            portfolio_config["channel_id"] = str(cfg["channel_id"])
        comps = cfg.get("components")
        portfolio_config["components"] = comps if isinstance(comps, list) else []
        portfolio_config["allowed_role_ids"] = [str(x) for x in (cfg.get("allowed_role_ids") or []) if x]
        print(f"[Config] portfolio — channel {portfolio_config['channel_id']} design {len(portfolio_config['components'])} roles {portfolio_config['allowed_role_ids']}")
    elif feature in ("orderlog", "customs-orderlog"):
        # Order Log is a second ticket panel: its buttons open tickets in their
        # own category with their own access roles (set per Ticket/Form button).
        # We register it as an additional ticket source — the shared handlers open
        # its tickets independently from the main Tickets panel.
        panels = _parse_ticket_panels(cfg)
        edited_ch = str(cfg.get("panel_channel_id") or (panels[0]["channel_id"] if panels else ""))
        _ticket_sources["customs-orderlog"] = {"panels": panels, "types": _parse_ticket_types(cfg)}
        _rebuild_ticket_registry()
        orderlog_gate["role_ids"] = [str(x) for x in (cfg.get("allowed_role_ids") or []) if x]
        orderlog_gate["keys"] = _collect_ticket_keys([p["components"] for p in panels])
        print(f"[Config] orderlog — panels {len(panels)} types {len(_ticket_sources['customs-orderlog']['types'])} panel_ch {edited_ch} gate_roles {orderlog_gate['role_ids']} gated_keys {len(orderlog_gate['keys'])}")
        if post_panel:
            await post_ticket_panel(only_channel_id=edited_ch or None)
    elif feature in ("order-status", "customs-order-status"):
        order_status_config["title"] = str(cfg.get("title") or "Order Status")
        try:
            order_status_config["limited_at"] = int(cfg.get("limited_at") or 8)
        except Exception:
            order_status_config["limited_at"] = 8
        try:
            order_status_config["closed_at"] = int(cfg.get("closed_at") or 10)
        except Exception:
            order_status_config["closed_at"] = 10
        order_status_config["emoji_open"] = str(cfg.get("emoji_open") or "")
        order_status_config["label_open"] = str(cfg.get("label_open") or "Open")
        order_status_config["emoji_limited"] = str(cfg.get("emoji_limited") or "")
        order_status_config["label_limited"] = str(cfg.get("label_limited") or "Oversite+ Only")
        order_status_config["emoji_closed"] = str(cfg.get("emoji_closed") or "")
        order_status_config["label_closed"] = str(cfg.get("label_closed") or "Closed")
        order_status_config["services"] = _parse_order_services(cfg.get("services"))
        print(f"[Config] order-status — {len(order_status_config['services'])} services limited@{order_status_config['limited_at']} closed@{order_status_config['closed_at']}")
    elif feature in ("pricing", "customs-pricing"):
        pricing_config["designer_role_ids"] = [str(x) for x in (cfg.get("designer_role_ids") or []) if x]
        pricing_config["currency"] = str(cfg.get("currency") or "$")
        pricing_config["title"] = str(cfg.get("title") or "Pricing")
        pricing_config["services"] = _parse_pricing_services(cfg.get("services"))
        comps = cfg.get("components")
        pricing_config["components"] = comps if isinstance(comps, list) else []
        # Pull the prices designers have set (persisted server-side).
        res = await _pricing_call("get")
        priced_services = 0
        if isinstance(res, dict) and res.get("ok"):
            pricing_config["values"] = res.get("prices") or {}
            priced_services = len(pricing_config["values"])
        else:
            print(f"[Pricing] price load FAILED (is the 'pricing' edge function deployed?): {(res or {}).get('error')}")
        print(f"[Config] pricing — {len(pricing_config['services'])} services, design {len(pricing_config['components'])}, {priced_services} services priced, roles {pricing_config['designer_role_ids']}")
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
        # Roles to add — new multi shape, with legacy single verified_role_id fallback.
        add_ids = cfg.get("verified_role_ids")
        if not isinstance(add_ids, list):
            add_ids = [cfg.get("verified_role_id")] if cfg.get("verified_role_id") else []
        roblox_config["verified_role_ids"] = [str(r) for r in add_ids if r]
        rem_ids = cfg.get("remove_role_ids")
        roblox_config["remove_role_ids"] = [str(r) for r in rem_ids if r] if isinstance(rem_ids, list) else []
        roblox_config["set_nickname"] = bool(cfg.get("set_nickname", True))
        roblox_config["log_channel_id"] = str(cfg.get("log_channel_id") or "")
        roblox_config["client_id"] = str(cfg.get("roblox_client_id") or "")
        roblox_config["client_secret"] = str(cfg.get("roblox_client_secret") or "")
        comps = cfg.get("components")
        roblox_config["components"] = comps if isinstance(comps, list) else []
        roblox_config["button_label"] = str(cfg.get("verify_button_label") or "Verify")
        roblox_config["button_style"] = str(cfg.get("verify_button_style") or "primary")
        print(f"[Config] roblox-verify — channel {roblox_config['channel_id']} add_roles {roblox_config['verified_role_ids']} remove_roles {roblox_config['remove_role_ids']} nick {roblox_config['set_nickname']} components {len(roblox_config['components'])}")
        # Post the panel when this came from a save/apply (deliberate action),
        # but NOT on boot — that avoids the surprise repost on every restart.
        # _replace_panel dedupes so a re-post replaces the old panel.
        if post_panel:
            await post_verify_panel()


def _is_tracked_giveaway_message(mid):
    """True if a message id belongs to a giveaway this process is tracking, so no
    panel-replacement logic can ever delete a giveaway message by mistake."""
    try:
        mid = str(mid)
        return any(str(g.get("message_id")) == mid for g in active_giveaways.values())
    except Exception:
        return False


async def _replace_panel(new_channel_id, new_message_id):
    """Record the freshly-posted panel and delete the previous one, so posting
    again REPLACES the old panel instead of stacking duplicates."""
    old = roblox_config.get("panel_ref")
    roblox_config["panel_ref"] = (
        {"channel_id": str(new_channel_id), "message_id": str(new_message_id)}
        if new_message_id and new_message_id is not True
        else None
    )
    if old and old.get("message_id") and not _is_tracked_giveaway_message(old["message_id"]):
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


async def _replace_robux_panel(new_channel_id, new_message_id):
    """Replace the previous Robux Locker panel so a re-save doesn't stack duplicates."""
    old = robux_locker_config.get("panel_ref")
    robux_locker_config["panel_ref"] = (
        {"channel_id": str(new_channel_id), "message_id": str(new_message_id)}
        if new_message_id and new_message_id is not True else None
    )
    if old and old.get("message_id") and not _is_tracked_giveaway_message(old["message_id"]):
        try:
            ch = await resolve_channel(old.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(int(old["message_id"]))
                await msg.delete()
        except Exception:
            pass


def _robux_render_components():
    """The saved design with live tokens filled in: {stock} = Available Stock,
    {funds} = the group balance last read by /robuxlocker."""
    comps = robux_locker_config.get("components") or []
    if not comps:
        return None
    raw = json.dumps(comps)
    raw = raw.replace("{stock}", str(int(robux_locker_config.get("stock") or 0)))
    raw = raw.replace("{funds}", str(int(robux_locker_config.get("last_funds") or 0)))
    try:
        return json.loads(raw)
    except Exception:
        return comps


async def post_robux_locker_panel():
    """(Re)post the Robux Locker panel from the dashboard design."""
    ch = await resolve_channel(robux_locker_config.get("channel_id"))
    if not ch:
        print("[RobuxLocker] no channel configured")
        return
    comps = _robux_render_components()
    if not comps:
        print("[RobuxLocker] no design saved — nothing to post")
        return
    try:
        mid = await send_v2_message(ch, comps)
        if mid:
            print("[RobuxLocker] panel posted")
            await _replace_robux_panel(ch.id, mid)
    except Exception as e:
        print(f"[RobuxLocker] panel post failed: {e}")


async def _robux_update_panel():
    """Edit the live panel in place so {stock} reflects the current Available
    Stock (called after /robuxlocker stocks it or a member buys)."""
    ref = robux_locker_config.get("panel_ref") or {}
    mid = ref.get("message_id")
    ch = await resolve_channel(ref.get("channel_id"))
    if not (mid and ch):
        return
    comps = _robux_render_components()
    if not comps:
        return
    guild = getattr(ch, "guild", None)
    built = [b for b in (_build_v2(c, guild) for c in comps) if b]
    if not built:
        return
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    if not {c.get("type") for c in built}.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    try:
        route = discord.http.Route(
            "PATCH", "/channels/{channel_id}/messages/{message_id}",
            channel_id=int(ch.id), message_id=int(mid),
        )
        await bot.http.request(route, json={"components": built})
    except Exception as e:
        print(f"[RobuxLocker] panel update failed: {e}")


async def _robux_locker_call(action, amount=0, time_frame=None):
    """POST to the robux-locker edge function (funds / stock / rate ops).
    `amount` may be fractional (the rate is dollars per 1k, e.g. 7.5)."""
    payload = {"action": action, "amount": amount}
    if time_frame:
        payload["timeFrame"] = time_frame
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/robux-locker",
                headers=_fn_headers(),
                json=payload,
                timeout=20,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200:
                return data
            return {"error": data.get("error") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _replace_ticket_panel(new_channel_id, new_message_id):
    """Record the freshly-posted ticket panel per channel. Every save re-posts
    all panels, so we replace the previous message IN THE SAME channel (no
    duplicate stacking) while panels in other channels are untouched — you keep
    as many panels as you have channels."""
    refs = ticket_config.get("panel_refs")
    if not isinstance(refs, dict):
        refs = {}
        ticket_config["panel_refs"] = refs
    ch_key = str(new_channel_id)
    old_mid = refs.get(ch_key)
    if new_message_id and new_message_id is not True:
        refs[ch_key] = str(new_message_id)
    if old_mid and not _is_tracked_giveaway_message(old_mid):
        try:
            ch = await resolve_channel(ch_key)
            if ch:
                msg = await ch.fetch_message(int(old_mid))
                await msg.delete()
        except Exception:
            pass


async def post_ticket_panel(only_channel_id=None):
    """(Re)post ticket panels. With only_channel_id set (a save while editing one
    panel), post JUST that panel and leave the others untouched. Without it,
    post every configured panel."""
    panels = ticket_config.get("panels")
    if not isinstance(panels, list) or not panels:
        panels = [{"channel_id": ticket_config.get("panel_channel_id"), "components": ticket_config.get("panel_components") or []}]
    target = str(only_channel_id) if only_channel_id else None
    for p in panels:
        if target and str(p.get("channel_id")) != target:
            continue
        ch = await resolve_channel(p.get("channel_id"))
        if not ch:
            continue
        await _post_one_panel(ch, p.get("components") or [])


async def _post_one_panel(ch, comps):
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

    # Fallback: a classic embed with an Open Ticket button per type — used only
    # when a panel has no custom design (or the custom one wouldn't send).
    types = ticket_config.get("types") or [{"id": "support", "name": "Support", "button_label": "Open Ticket", "button_style": "primary"}]
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

    # Roles: add the configured verify roles, remove the configured ones.
    add_ids = roblox_config.get("verified_role_ids") or []
    remove_ids = roblox_config.get("remove_role_ids") or []
    if not add_ids:
        notes.append("• No 'Roles to add on verify' is set in the dashboard — open the Verification block, pick one or more roles, and Save.")
        print("[Verify] no verified_role_ids configured")

    add_roles = [r for r in (guild.get_role(int(x)) for x in add_ids if str(x).isdigit()) if r]
    if add_roles:
        try:
            await member.add_roles(*add_roles, reason="Roblox verified")
            print(f"[Verify] added {[r.name for r in add_roles]} to {member}")
        except discord.Forbidden:
            notes.append("• Couldn't add one or more verify roles — my role must sit **above** them in Server Settings → Roles, and I need **Manage Roles**.")
            print("[Verify] add roles forbidden (hierarchy/perms)")
        except Exception as e:
            notes.append(f"• Couldn't add verify roles — {e}")
            print(f"[Verify] add roles failed: {e}")

    remove_roles = [r for r in (guild.get_role(int(x)) for x in remove_ids if str(x).isdigit()) if r and r in member.roles]
    if remove_roles:
        try:
            await member.remove_roles(*remove_roles, reason="Roblox verified")
            print(f"[Verify] removed {[r.name for r in remove_roles]} from {member}")
        except discord.Forbidden:
            notes.append("• Couldn't remove one or more roles — my role must sit **above** them in Server Settings → Roles, and I need **Manage Roles**.")
            print("[Verify] remove roles forbidden (hierarchy/perms)")
        except Exception as e:
            notes.append(f"• Couldn't remove roles — {e}")
            print(f"[Verify] remove roles failed: {e}")

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
    for feature in ("welcome", "invite", "tickets", "credits", "roblox-verify", "customs-giveaway", "customs-robux-locker", "customs-portfolio", "customs-orderlog", "customs-order-status", "customs-pricing"):
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
                timeout=20,
            )
        if r.status_code != 200:
            return
        rows = r.json()
    except httpx.TransportError:
        return  # transient network blip (timeout/connection) — retried next cycle
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
        elif isinstance(ch, discord.ForumChannel):
            ctype = "forum"
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
    # Flush every active giveaway (entrants + state) BEFORE we exit, so a redeploy
    # never drops anyone — the boot restore puts them all back.
    try:
        pending = list(active_giveaways.items())
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*[_gw_save_state(gid, g) for gid, g in pending], return_exceptions=True),
                timeout=8,
            )
            print(f"[Shutdown] flushed {len(pending)} giveaway(s) to storage")
    except Exception as e:
        print(f"[Shutdown] giveaway flush error: {e}")
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
            r = await client.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=15)
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except httpx.TransportError:
        pass  # transient network blip (timeout/connection) — retried next cycle
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
