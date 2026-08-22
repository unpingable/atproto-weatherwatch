#!/usr/bin/env bash
# Publish the rendered weatherwatch report to the static web tier.
#
# Pushes rendered HTML only. The collector never runs on the web host, never
# talks to Labelwatch, and does not care whether this script ever succeeds —
# collection and serving are separate concerns and stay that way.
#
# Replacement is atomic at the rename: the incoming tree is staged beside the
# live one and swapped in, so a reader gets the whole old report or the whole
# new one, never a half-written mix. This mirrors what Labelwatch already does
# for its own webroot.
#
#   ./deploy/publish.sh                       # regenerate + publish (remote)
#   ./deploy/publish.sh --skip-generate       # publish what is already built
#   WW_MODE=local ./deploy/publish.sh         # publish on the serving host itself
#
# Two modes, one privacy gate and one atomic-swap discipline:
#   remote  push rendered output from a workstation over ssh (original path)
#   local   render and swap on the serving host (used by the systemd timer)
#
# Env:
#   WW_MODE      remote | local           (default remote)
#   WW_DB        SQLite path              (default data/weatherwatch.sqlite)
#   WW_BUILD     render dir               (default build/report)
#   WW_TARGET    live directory (local)   (default /var/www/weatherwatch)
#   WW_SSH_HOST  target (remote)          (default root@labelwatch.neutral.zone)
#   WW_SSH_KEY   identity (remote)        (default ~/.ssh/linode)
#   WW_REMOTE    live directory (remote)  (default /var/www/weatherwatch)
#   WW_URL       verification URL         (empty in local mode = skip HTTP check)
#   WW_PY        python for rendering     (default python3)
#   WW_CLI       console-script path      (preferred over WW_PY; see below)
#   WW_PUBLIC_URL canonical URL; only when set are share-card
#                 meta tags + og-card.png emitted

set -euo pipefail

MODE="${WW_MODE:-remote}"
DB="${WW_DB:-data/weatherwatch.sqlite}"
# Episode store for the report's social section. Aggregate-tier episodes only
# reach the page; they are derived from the identity-free minute counters and
# carry no actor or target. Unset means the section renders as "no episode
# store configured" rather than disappearing -- absence of a section would be
# indistinguishable from absence of episodes.
SOCIAL_DB="${WW_SOCIAL_DB:-}"
BUILD="${WW_BUILD:-build/report}"
TARGET="${WW_TARGET:-/var/www/weatherwatch}"
SSH_HOST="${WW_SSH_HOST:-root@labelwatch.neutral.zone}"
SSH_KEY="${WW_SSH_KEY:-$HOME/.ssh/linode}"
REMOTE="${WW_REMOTE:-/var/www/weatherwatch}"
PY="${WW_PY:-python3}"
CLI="${WW_CLI:-}"

# Prefer the installed console script over `python -m` when one is configured.
#
# `python -m pkg` prepends the *current working directory* to sys.path, so any
# stray `weatherwatch/` directory sitting in WorkingDirectory silently shadows
# the installed package — the deployed units run from /opt/weatherwatch, which
# is exactly where a mis-aimed rsync lands. Verified 2026-08-11: a stray package
# in cwd wins over the editable install. A console script's sys.path[0] is its
# own bin directory, so cwd never enters the search path.
#
# PYTHONSAFEPATH / `python -P` would also fix this but are 3.11+; the serving
# host is 3.10. Falls back to `python -m` so local and offline runs are
# unaffected.
if [ -n "$CLI" ]; then
  RENDER=("$CLI")
else
  RENDER=("$PY" -m weatherwatch.cli)
fi
if [ "$MODE" = "local" ]; then
  URL="${WW_URL:-}"
else
  URL="${WW_URL:-https://labelwatch.neutral.zone/weatherwatch}"
fi
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST")

if [ "${1:-}" != "--skip-generate" ]; then
  echo "==> Rendering from $DB"
  "${RENDER[@]}" --db "$DB" report --output "$BUILD" \
      ${SOCIAL_DB:+--social-db "$SOCIAL_DB"} \
      ${WW_PUBLIC_URL:+--public-url "$WW_PUBLIC_URL"}
fi

[ -f "$BUILD/index.html" ] || { echo "!! no $BUILD/index.html" >&2; exit 1; }

# Refuse to publish anything carrying user identity. The generator cannot
# produce it, but this is the last gate before bytes leave the machine and it
# costs nothing to keep.
# The `a:[0-9a-f]{12}` arm covers salted actor tokens, which the social lane
# puts on edge-tier findings. Those findings are excluded from the published
# projection by detector allowlist, so this arm should never fire — which is
# exactly why it is here: a gate that only checks what you expect to go wrong
# is not a gate.
IDENT_RE="did:(plc|web|key):|at://|bafy[a-z0-9]{10,}|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b"
echo "==> Privacy gate"
if grep -rEq "$IDENT_RE" "$BUILD"; then
  echo "!! identity-shaped value found in $BUILD — refusing to publish" >&2
  grep -rEno "$IDENT_RE" "$BUILD" >&2 | head
  exit 1
fi
echo "    clean"

if [ "$MODE" = "local" ]; then
  STAGE="${TARGET}.incoming"
  PREV="${TARGET}.prev"
  echo "==> Staging to $STAGE"
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -a "$BUILD/." "$STAGE/"
  echo "==> Atomic swap"
  rm -rf "$PREV"
  # NB: an `[ -d ] && mv` one-liner would abort under `set -e` on first run,
  # when the target does not exist yet.
  if [ -d "$TARGET" ]; then mv "$TARGET" "$PREV"; fi
  mv "$STAGE" "$TARGET"
  chmod -R a+rX "$TARGET"
  rm -rf "$PREV"
else
  STAGE="${REMOTE}.incoming"
  PREV="${REMOTE}.prev"
  echo "==> Staging to ${SSH_HOST}:${STAGE}"
  "${SSH[@]}" "rm -rf '$STAGE' && mkdir -p '$STAGE'"
  rsync -a --delete -e "ssh -i $SSH_KEY -o BatchMode=yes" "$BUILD/" "$SSH_HOST:$STAGE/"

  echo "==> Atomic swap"
  "${SSH[@]}" "set -e
    rm -rf '$PREV'
    if [ -d '$REMOTE' ]; then mv '$REMOTE' '$PREV'; fi
    mv '$STAGE' '$REMOTE'
    chmod -R a+rX '$REMOTE'
    rm -rf '$PREV'"
fi

if [ -n "$URL" ]; then
  echo "==> Verifying $URL"
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$URL/")
  echo "    HTTP $code"
  [ "$code" = "200" ] || { echo "!! expected 200" >&2; exit 1; }
fi
echo "==> Published."
