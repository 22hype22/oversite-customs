#!/usr/bin/env bash
# Mirror this codebase into 22hype22/oversite-roleplay (the Oversite Roleplay
# bot). The two repos are the same code; the only difference is BOT_BASE
# defaulting to "roleplay" over there. Run from a checkout that can push to
# both repos:
#
#   bash deploy/sync-roleplay.sh            # clones, copies, commits, pushes
#   bash deploy/sync-roleplay.sh --no-push  # stop before pushing
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${ROLEPLAY_DIR:-/tmp/oversite-roleplay-sync}"
REPO="https://github.com/22hype22/oversite-roleplay"

if [[ ! -d "$WORK/.git" ]]; then
  git clone -q "$REPO" "$WORK"
fi
cd "$WORK"
git checkout -q main
git pull -q --ff-only origin main || true

# Everything the bot needs to run, nothing customs-specific (no deploy/ VPS
# scripts, no SQL migrations).
rm -rf "$WORK/music"
cp "$SRC/main.py" "$SRC/requirements.txt" "$SRC/Procfile" "$SRC/nixpacks.toml" "$SRC/runtime.txt" "$WORK/"
cp -r "$SRC/music" "$WORK/music"
rm -rf "$WORK/music/__pycache__"
printf '__pycache__/\n*.pyc\n.venv/\n.env\n' > "$WORK/.gitignore"
[[ -f "$WORK/d" ]] && git rm -q -f d || true

python3 - "$WORK/main.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
a = 'BOT_BASE = (os.getenv("BOT_BASE") or "customs").strip().lower()'
b = 'BOT_BASE = (os.getenv("BOT_BASE") or "roleplay").strip().lower()'
if a not in s:
    raise SystemExit("BOT_BASE default line not found; main.py layout changed")
open(p, "w").write(s.replace(a, b))
PY

if [[ ! -f "$WORK/CLAUDE.md" ]]; then
  cat > "$WORK/CLAUDE.md" <<'EOF'
# CLAUDE.md — Oversite Roleplay bot

Single-file discord.py bot (`main.py`). Auto-deploys to Railway from `main`.

This is the Oversite Roleplay copy of the Network codebase
(github.com/22hype22/oversite-customs), refreshed by deploy/sync-roleplay.sh
there. Do not edit here by hand: change oversite-customs, then run the sync.
`BOT_BASE` (default "roleplay") decides the brand name, which slash commands
survive the sync, and which dashboard blocks load.

## Message wording rules

No emoji or symbol glyphs, no em dashes, no parentheses, no AI voice.
Ratings are written out: "4 out of 5".
EOF
fi

python3 -m py_compile "$WORK/main.py"
git add -A
SRC_SHA="$(git -C "$SRC" rev-parse --short HEAD)"
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git commit -q -m "Sync from oversite-customs ${SRC_SHA}"
fi
if [[ "${1:-}" == "--no-push" ]]; then
  echo "Committed in $WORK (not pushed)."
  exit 0
fi
# Push whatever is ahead of origin, including commits from an earlier run.
git push -q -u origin main
echo "oversite-roleplay main is up to date with oversite-customs ${SRC_SHA}"
