# Deploy Oversite Customs with IPv6 /64 rotation (the permanent fix)

This runs the bot on a cheap VPS that has a routed IPv6 **/64** (18 quintillion
addresses). The bot sends every YouTube request from a **different** address in
that block, so no single IP ever trips YouTube's bot-check and the block can't
be applied to the whole /64. This is how music bots avoid "Sign in to confirm
you're not a bot" — set up once, then leave it alone.

## 1. Get a server with an IPv6 /64
**Hetzner Cloud** is the easy pick — every server gets a /64 automatically.
1. Sign up at <https://console.hetzner.cloud>.
2. **New Project** → **Add Server**.
3. Location: any. Image: **Ubuntu 24.04**.
4. Type: the cheapest shared **CX22** (~€4/mo) is plenty.
5. Make sure **IPv6** is enabled (it is by default).
6. Add your SSH key (or set a root password), then **Create**.
7. Note the server's IPv4 address for SSH.

## 2. SSH in
```
ssh root@YOUR_SERVER_IP
```

## 3. Clone, configure, run
```bash
git clone https://github.com/22hype22/oversite-customs /opt/oversite-customs
cd /opt/oversite-customs
cp deploy/.env.example deploy/.env
nano deploy/.env        # paste DISCORD_TOKEN, BOT_ORDER_ID, WORKER_TOKEN
sudo bash deploy/setup.sh
```
Get the three secrets from **Railway → your bot service → Variables** (copy the
values). Leave `IPV6_SUBNET` blank — it auto-detects.

The script installs everything, configures IPv6 rotation, and starts the bot as
a service that survives reboots.

## 4. Verify
```
journalctl -u oversite-customs -f
```
You want to see:
```
[Boot] yt-dlp 2026.08.19 — /play: ... | ipv6_rotate=on (....::/64) | ...
[Boot] egress IP (ipv6) = 2a01:4f8:....  <-- rotates per request
```
Then run `/play` and `/radio` in Discord. Done.

## Everyday commands
| Task | Command |
|---|---|
| Watch logs | `journalctl -u oversite-customs -f` |
| Restart | `systemctl restart oversite-customs` |
| Update to latest code | `cd /opt/oversite-customs && git pull && systemctl restart oversite-customs` |
| Stop | `systemctl stop oversite-customs` |

## Turn OFF Railway
Once this VPS is running, **delete/stop the Railway deployment** so you don't run
two copies of the same bot (they'd both respond). Keep one, not both.

## Troubleshooting
- `IPV6 ROTATION TEST FAILED` in the log → the server has no IPv6 /64, or AnyIP
  routing didn't apply. Re-run `sudo bash deploy/setup.sh`. On Hetzner, confirm
  IPv6 is enabled on the server's Networking tab.
- Bot won't start → check the three secrets in `deploy/.env` are correct.
