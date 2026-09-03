# CLAUDE.md — Oversite Network bot

Single-file discord.py bot (`main.py`). Auto-deploys to Railway from `main`.

## Message wording rules (owner's standing request)

Every message the bot posts or DMs must read like a person typed it:

- No emoji and no symbol glyphs (no stars, checkmarks, arrows). Ratings are
  written out: "4 out of 5", "4.8 out of 5 from 12 vouches".
- No em dashes, no parentheses, no mid-dot separators. Use commas and short
  sentences instead.
- No "AI voice": no "Nice try", no cheerleading, no filler. Say what happened.
- Existing red/green channel-name markers for tickets are the one exception.

Dashboard-designable versions of the system messages live under the System
Messages block keys (ticket_progress, ticket_queue_update,
ticket_staff_reminder, ticket_designer_away, ticket_designer_back, ...).

## Persistence

Anything the bot must remember across a redeploy goes through
`_durable_config_get(feature)` / `_bot_config_upsert(feature, config)` and a
`*_loaded` flag so a failed load never overwrites stored data with an empty
snapshot (see vouch-data, sales-data, away-data, ticket-autoclose-state).

## Git

Commit trailers are set by the session. Never put model names in commits,
comments, or code.
