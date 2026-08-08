import os
import io
import re
import json
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
}
credits_config = {"manager_role_ids": CREDIT_MANAGER_ROLE_IDS, "currency_name": "credits", "log_channel_id": ""}
_credits_memory = {}


def success_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0x57F287)


def error_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0xED4245)


def info_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=ACCENT)


def _fn_headers():
    return {"x-worker-token": WORKER_TOKEN, "Content-Type": "application/json", "apikey": SUPABASE_KEY}


_poll_session = None


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

    if BOT_ORDER_ID and WORKER_TOKEN:
        for loop in (send_heartbeat, poll_configs, poll_shutdown, record_metrics_loop):
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


def _resolve_emoji_shortcodes(text, guild):
    if ":" not in text or not guild:
        return text
    lookup = {e.name.lower(): e for e in guild.emojis}
    if not lookup:
        return text

    def repl(match):
        emoji = lookup.get(match.group(1).lower())
        if emoji is None:
            return match.group(0)
        return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"

    return _EMOJI_SHORTCODE_RE.sub(repl, text)


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
    if interaction.type != discord.InteractionType.component:
        return
    cid = (interaction.data or {}).get("custom_id", "")
    if cid == "ticket_select":
        values = (interaction.data or {}).get("values") or []
        if values:
            await open_ticket(interaction, values[0])
    elif cid.startswith("ticket_cat:"):
        await open_ticket(interaction, cid.split(":", 1)[1])
    elif cid == "ticket_open":
        await open_ticket(interaction, "support")
    elif cid.startswith("ticket_close"):
        await close_ticket(interaction)


def _ticket_topic(opener_id, category):
    return f"ticket|{opener_id}|{category}"


async def open_ticket(interaction, category):
    guild = interaction.guild
    if not guild:
        return
    await interaction.response.defer(ephemeral=True)

    if ticket_config.get("one_per_user", True):
        for ch in guild.text_channels:
            topic = ch.topic or ""
            if topic.startswith("ticket|") and topic.split("|")[1] == str(interaction.user.id):
                await interaction.followup.send(embed=error_embed("Ticket already open", f"You already have an open ticket: {ch.mention}"), ephemeral=True)
                return

    category_channel = None
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

    base_name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:90]
    try:
        channel = await guild.create_text_channel(
            name=base_name,
            category=category_channel if isinstance(category_channel, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=_ticket_topic(interaction.user.id, category),
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

    open_msg = ticket_config.get("open_message") or f"Thanks {interaction.user.mention}, a member of the team will be with you shortly."
    open_msg = open_msg.replace("{user}", interaction.user.mention)
    embed = info_embed(f"{category.title()} ticket", open_msg)
    embed.set_footer(text=f"Opened by {interaction.user}")

    close_view = discord.ui.View(timeout=None)
    close_view.add_item(discord.ui.Button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close", emoji="🔒"))

    content = " ".join(filter(None, [interaction.user.mention, ping])) or None
    await channel.send(content=content, embed=embed, view=close_view)
    await record_ticket(guild.id, channel.id, interaction.user.id, category, "open")
    await interaction.followup.send(embed=success_embed("Ticket opened", f"Your ticket is ready: {channel.mention}"), ephemeral=True)


async def close_ticket(interaction):
    channel = interaction.channel
    topic = getattr(channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        await interaction.response.send_message(embed=error_embed("Not a ticket", "This channel isn't a ticket."), ephemeral=True)
        return
    parts = topic.split("|")
    opener_id = parts[1] if len(parts) > 1 else ""
    category = parts[2] if len(parts) > 2 else "support"

    is_support = has_any_role(interaction.user, ticket_config.get("support_role_ids", []))
    is_opener = str(interaction.user.id) == opener_id
    if not (is_support or is_opener or interaction.user.guild_permissions.manage_channels):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff or the opener can close this."), ephemeral=True)
        return

    await interaction.response.send_message(embed=info_embed("Closing ticket", "Saving transcript and closing in a moment."))
    transcript = await build_transcript(channel)
    log_id = ticket_config.get("log_channel_id") or ""
    opener = interaction.guild.get_member(int(opener_id)) if opener_id.isdigit() else None
    if log_id:
        log_channel = interaction.guild.get_channel(int(log_id))
        if log_channel:
            embed = info_embed("Ticket closed", f"**Category:** {category}\n**Opened by:** {opener.mention if opener else opener_id}\n**Closed by:** {interaction.user.mention}")
            try:
                await log_channel.send(embed=embed, file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{channel.name}.txt"))
            except Exception as e:
                print(f"[Ticket] log failed: {e}")
    await record_ticket(interaction.guild.id, channel.id, opener_id, category, "closed")
    await asyncio.sleep(3)
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception as e:
        print(f"[Ticket] delete failed: {e}")


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


async def send_v2_message(channel, components_v2, content=None):
    def build(comp):
        ctype = comp.get("type", "")
        if ctype in ("text", "text_display"):
            text = comp.get("text") or comp.get("content", "")
            title = comp.get("title", "")
            if title:
                text = f"**{title}**\n{text}" if text else f"**{title}**"
            return {"type": 10, "content": text} if text else None
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
            thumb = comp.get("thumbnailUrl") or comp.get("thumbnail_url")
            button = comp.get("button")
            children = [{"type": 10, "content": text}] if text else []
            if not children:
                return None
            obj = {"type": 9, "components": children}
            if thumb and str(thumb).startswith("http"):
                obj["accessory"] = {"type": 11, "media": {"url": thumb}}
            elif isinstance(button, dict) and button.get("label"):
                obj["accessory"] = build_button(button, getattr(channel, "guild", None))
            return obj
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
                if category:
                    has_category = True
                    value = category
                elif channel_id:
                    value = f"ch:{channel_id}"
                elif url:
                    value = f"url:{url}"[:100]
                else:
                    value = label[:100]
                o = {"label": label[:100], "value": value[:100]}
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
    top_types = {c.get("type") for c in built}
    if not top_types.issubset({10, 14, 17}) or (17 in top_types and len(top_types) > 1):
        built = [{"type": 17, "components": built}]
    payload = {"components": built, "flags": 1 << 15}
    if content:
        payload["content"] = content
    route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    try:
        await bot.http.request(route, json=payload)
        return True
    except Exception as e:
        print(f"[V2] send failed: {e}")
        return False


def build_button(btn, guild):
    label = btn.get("label", "Button")
    category = btn.get("category", "")
    channel_id = btn.get("channel_id", "")
    url = btn.get("url", "")
    style_name = str(btn.get("style", "primary")).lower()
    if category:
        return {"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_cat:{category[:80]}"}
    if channel_id:
        gid = getattr(guild, "id", 0)
        return {"type": 2, "label": label[:80], "style": 5, "url": f"https://discord.com/channels/{gid}/{channel_id}"}
    if url:
        return {"type": 2, "label": label[:80], "style": 5, "url": url}
    return {"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"btn_{label[:20]}"}


def build_embed(data):
    try:
        color = int(data.get("color")) if data.get("color") is not None else ACCENT
    except Exception:
        color = ACCENT
    embed = discord.Embed(color=color)
    if data.get("title"):
        embed.title = data["title"]
    if data.get("title_url"):
        embed.url = data["title_url"]
    if data.get("description"):
        embed.description = data["description"]
    author = data.get("author")
    if isinstance(author, dict) and author.get("name"):
        embed.set_author(name=author["name"], icon_url=author.get("icon_url") or None)
    footer = data.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        embed.set_footer(text=footer["text"], icon_url=footer.get("icon_url") or None)
    for f in data.get("fields", []) or []:
        if f.get("name") and f.get("value"):
            embed.add_field(name=f["name"], value=f["value"], inline=bool(f.get("inline")))
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
    content = payload.get("content") or None
    embeds = [build_embed(e) for e in embeds_data if isinstance(e, dict)]
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


async def apply_config(feature, cfg):
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
        print(f"[Config] tickets — category {ticket_config['category_id']} roles {ticket_config['support_role_ids']}")
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
    for feature in ("welcome", "invite", "tickets", "credits"):
        cfg = await fetch_config(feature)
        if cfg:
            await apply_config(feature, cfg)


async def complete_command(command_id, status="done", error=None):
    body = {"command_id": command_id, "status": status}
    if error:
        body["error_message"] = error
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/complete-command", headers=_fn_headers(), json=body)
    except Exception as e:
        print(f"[Command] complete failed: {e}")


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
                    await apply_config(feature, cfg)
                await mark_config_applied(feature)
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


@tasks.loop(hours=2)
async def sync_identity():
    await apply_bot_identity()


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
    for loop in (send_heartbeat, poll_configs, record_metrics_loop):
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
        print(f"[Shutdown] claim error: {e}")
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
