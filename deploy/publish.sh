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
#   WW_BUILD     render dir               (default build/beef)
#   WW_TARGET    live directory (local)   (default /var/www/weatherwatch-beef)
#   WW_SSH_HOST  target (remote)          (default root@labelwatch.neutral.zone)
#   WW_SSH_KEY   identity (remote)        (default ~/.ssh/linode)
#   WW_REMOTE    live directory (remote)  (default /var/www/weatherwatch-beef)
#   WW_URL       verification URL         (empty in local mode = skip HTTP check)
#   WW_PY        python for rendering     (default python3)

set -euo pipefail

MODE="${WW_MODE:-remote}"
DB="${WW_DB:-data/weatherwatch.sqlite}"
BUILD="${WW_BUILD:-build/beef}"
TARGET="${WW_TARGET:-/var/www/weatherwatch-beef}"
SSH_HOST="${WW_SSH_HOST:-root@labelwatch.neutral.zone}"
SSH_KEY="${WW_SSH_KEY:-$HOME/.ssh/linode}"
REMOTE="${WW_REMOTE:-/var/www/weatherwatch-beef}"
PY="${WW_PY:-python3}"
if [ "$MODE" = "local" ]; then
  URL="${WW_URL:-}"
else
  URL="${WW_URL:-https://labelwatch.neutral.zone/beef}"
fi
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST")

if [ "${1:-}" != "--skip-generate" ]; then
  echo "==> Rendering from $DB"
  "$PY" -m weatherwatch.cli --db "$DB" report --output "$BUILD"
fi

[ -f "$BUILD/index.html" ] || { echo "!! no $BUILD/index.html" >&2; exit 1; }

# Refuse to publish anything carrying user identity. The generator cannot
# produce it, but this is the last gate before bytes leave the machine and it
# costs nothing to keep.
echo "==> Privacy gate"
if grep -rEq "did:(plc|web|key):|at://|bafy[a-z0-9]{10,}|[a-z0-9-]+\.bsky\.(social|app)" "$BUILD"; then
  echo "!! identity-shaped value found in $BUILD — refusing to publish" >&2
  grep -rEno "did:(plc|web|key):|at://|bafy[a-z0-9]{10,}|[a-z0-9-]+\.bsky\.(social|app)" "$BUILD" >&2 | head
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
