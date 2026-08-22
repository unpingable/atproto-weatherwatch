# Deploy — live (dark)

Status as of 2026-08-09:

| | |
|---|---|
| `/var/www/weatherwatch` on the web host | created, populated |
| `deploy/publish.sh` (render → privacy gate → atomic swap) | working, verified under load |
| Caddy route for `/weatherwatch` | **applied**, scoped to `/weatherwatch` and `/beef/*` only |
| `https://weatherwatch.neutral.zone/` | **200** |
| Existing Labelwatch behaviour | unchanged (verified byte-identical against the pre-deployment baseline) |

Backups taken, in order: `Caddyfile.bak.20260808-195841` (route added),
`Caddyfile.bak.20260808-210057` (matcher tightened).

## What the web tier actually is

Inspected live, not inferred:

* Caddy 2.10.0 in a container named `caddy`, serving **7 site blocks** from a
  single 86-line file: host `/home/jbeck/atproto/Caddyfile` → container
  `/etc/caddy/Caddyfile`. No `import` directives, so there is no drop-in
  conf.d to add a file to.
* The existing static-directory pattern is host `/var/www/<name>` →
  container `/srv/www/<name>` (bind-mounted **read-only**) plus
  `root * /srv/www/<name>` + `file_server`. Used by `labelwatch`, `lexidoku`
  and `stechometer`. `/weatherwatch` uses exactly this pattern — no new service, no
  new mount, no reverse proxy.
* Labelwatch's own block already mixes a `handle @api` group with a trailing
  `file_server`, so adding one more mutually-exclusive `handle_path` group is
  the same shape as what is already there.
* The Caddyfile is not under version control; the host convention is
  timestamped `Caddyfile.bak.YYYYMMDD-HHMMSS` copies, made by root.

## The applied change

One additive block inside the existing
`labelwatch.sp00ky.net, labelwatch.neutral.zone { … }` site, after the
`handle @api` group (the snippet also lives at `deploy/beef-route.caddy`):

```caddyfile
    # Weatherwatch aggregate report (siloed; nothing links here, noindex).
    # Static only: separate root, separate publish path, no service.
    @beef path /beef /beef/*
    handle @beef {
        root * /srv/www/weatherwatch-beef
        header X-Robots-Tag "noindex, nofollow, noarchive"
        uri strip_prefix /beef
        file_server
    }
```

The matcher is two exact patterns, not a prefix glob. `handle_path /beef*`
(the original form) matched anything *starting* with `/weatherwatch`, so `/beefsteak`
and `/beefy.html` were entering the Weatherwatch namespace and being answered
by its handler rather than Labelwatch's. `handle_path` accepts only one
matcher, hence the named matcher plus an explicit `uri strip_prefix`, which
keeps bare `/weatherwatch` answering 200 rather than redirecting.

Why this is additive rather than a behaviour change: `/weatherwatch` returned 404
before deployment (verified), so no existing route was redefined; `handle` groups are
mutually exclusive, and `/beef*` cannot overlap `/v1/*` or `/health`; the other
six site blocks are untouched. The existing `@html` / `@json` header rules are
evaluated before `handle`, so Labelwatch's own caching headers keep applying to
its own paths exactly as before.

The residual risk is not the route, it is the **reload**: Caddy is a shared
proxy for seven sites, and a malformed config would take all of them down.
Mitigated by validating before reloading, and by the backup.

### How it was applied (and how to re-apply)

```bash
SSH="ssh -i ~/.ssh/linode root@labelwatch.neutral.zone"

# 1. Back up, following the host's own convention
$SSH 'cp -a /home/jbeck/atproto/Caddyfile \
      /home/jbeck/atproto/Caddyfile.bak.$(date +%Y%m%d-%H%M%S)'

# 2. Insert the block above after the `handle @api { … }` group

# 3. Validate BEFORE reloading — this is the step that protects the other six sites
$SSH 'docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile'

# 4. Graceful, zero-downtime reload
$SSH 'docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile'

# 5. Verify
curl -sS -o /dev/null -w '%{http_code}\n' https://weatherwatch.neutral.zone/
curl -sS -o /dev/null -w '%{http_code}\n' https://labelwatch.neutral.zone/        # must stay 200
curl -sS -o /dev/null -w '%{http_code}\n' https://stechometer.neutral.zone/       # must stay 200
```

### To roll back

```bash
$SSH 'cp -a /home/jbeck/atproto/Caddyfile.bak.<stamp> /home/jbeck/atproto/Caddyfile \
      && docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile'
```

Removing `/var/www/weatherwatch` is optional; while no route points at it
it serves nothing.

## Path rename (2026-08-09)

Canonical path is `/weatherwatch`. `/beef` 301-redirects to it and will keep
doing so — existing references stay alive, and a 301 renders nothing, so it
cannot prime the misreading the rename exists to stop.

The joke name stays *on the page*, where the "this measures no conflict,
sentiment, users or content" denial sits next to it. A URL travels without its
disclaimer; a cold reader — human or model — meets `/beef` alone and concludes
social-drama analytics. That is the whole reason for the split.

Caddy orders `redir` **before** `uri`, so the obvious
`uri strip_prefix /beef` + `redir {uri}` pairing silently sends `/beef` to
`/weatherwatch/beef`. The route captures the tail with `path_regexp` instead —
the same shape the DID redirects in that file already use. Verified: `/beef`,
`/beef/`, `/beef/index.html` and `/beef/summary.json` all 301 to their
`/weatherwatch` equivalents and follow to 200, while `/beefsteak` is untouched.

Backup: `Caddyfile.bak.20260809-141216`.

## Share card

`og-card.png` plus Open Graph / Twitter tags are emitted **only** when a
canonical URL is configured (`WW_PUBLIC_URL`, set in the publisher unit).
Without it the report renders with no share metadata at all and stays entirely
self-contained — which is what local and offline renders get.

An unfurl card is the highest-risk cold-read surface there is: it is often all
the context a reader gets, it is cached by whoever unfurls it, and it reaches
people who never open the page. So the card leads with the denial —
*"Not conflict. Not sentiment. Not users. Not content."* — and the description
says the same in prose.

The image is a **fixed asset**, never re-rendered from live data. A share card
outlives the numbers printed on it; a cached card showing stale rates would
mislead exactly the audience least able to check. Source and regeneration
command are in `assets/og-card.src.html`.

This does not weaken the dark posture. Share tags govern how a link *you
deliberately share* renders; they do not make anything discoverable.
`noindex, nofollow, noarchive` still applies to the page and to the image, and
nothing links to either path.

## Witness declaration — gap noted, not improvised

Per "declare before you disturb", a Caddy reload on this host should be
declared to NQ first. NQ has no CLI or declaration surface **on this host** —
it is proxied from elsewhere via `172.17.0.1:9848` — so there was nothing to
declare to from here. Following the doctrine, no suppression was improvised
and the gap is recorded here instead. A graceful `caddy reload` is a config
swap with no connection drop, and Labelwatch's own witness requirements
already class config reloads as routine disturbances.


## Runtime (systemd, on the serving host)

Continuous collection is a **deployment choice, not a semantic requirement**.
The data model is an observation instrument: bounded sessions are exactly as
correct, and `weatherwatch collect --duration 30m` remains valid. Running
continuously simply keeps rolling baselines contiguous and keeps cursor replay
as recovery machinery rather than normal operation.

| | |
|---|---|
| Collector unit | `weatherwatch-collector.service` |
| Publisher unit | `weatherwatch-publish.service` (`Type=oneshot`) |
| Timer | `weatherwatch-publish.timer` — 5 min, `OnBootSec=2min` |
| Service user | `weatherwatch` (system, nologin, uid 997) |
| Code | `/opt/weatherwatch` (venv at `.venv`, Python 3.10) |
| Database | `/var/lib/weatherwatch/weatherwatch.sqlite` |
| Static output | `/var/www/weatherwatch/` |
| Source endpoint | `wss://jetstream1.us-east.bsky.network/subscribe` |

Exact commands:

```
collector  /opt/weatherwatch/.venv/bin/python -m weatherwatch.cli \
             --db /var/lib/weatherwatch/weatherwatch.sqlite \
             collect --endpoint wss://jetstream1.us-east.bsky.network/subscribe
publisher  /opt/weatherwatch/deploy/publish.sh      (WW_MODE=local)
```

The endpoint is written out in full in the unit and must match
`weatherwatch.collector.DEFAULT_ENDPOINT`. Changing it creates a hard
observation seam by design — a new run, a new cursor namespace, never a silent
continuation.

### Operating

```bash
systemctl status weatherwatch-collector
journalctl -u weatherwatch-collector -f          # STATS lines, one per window
journalctl -u weatherwatch-publish -n 50         # publication failures
systemctl restart weatherwatch-collector         # resumes at committed cursor+1
systemctl list-timers weatherwatch-publish.timer
systemctl start weatherwatch-publish.service     # publish now, out of band
```

Manual publish from a workstation still works (`./deploy/publish.sh`, remote
mode) but will overwrite the host-generated report with whatever the *local*
database holds. With the timer running, prefer `systemctl start
weatherwatch-publish.service` on the host.

### Measured on this host (2026-08-09, steady state)

| | |
|---|---|
| CPU | 5.7% of one core (60s sample, ~270 eps) |
| RSS | ~76 MB |
| Stream lag | 0.010s |
| Database | 2.2 MB after ~4h of aggregate history |
| Publication | ~0.2s per run, every 5 min |

RSS sits above the ~30–35 MB seen in bounded runs because a long backlog
replay retains allocator arenas; it is stable and far under `MemoryHigh=512M`.
During replay CPU runs near the `CPUQuota=50%` ceiling, which throttles
catch-up slightly — deliberate, and catch-up still completes (a 4.4h backlog
drained in roughly 12 minutes).

### Separation

The collector and publisher share nothing but the SQLite file, and neither
unit references the other — no `After=`, no `Requires=`. Verified: stopping
the timer leaves collection running; stopping the collector still lets the
publisher regenerate and `/weatherwatch` keeps serving the last report.

### Privileges

The collector holds only `ReadWritePaths=/var/lib/weatherwatch`. The publisher
additionally gets `ReadWritePaths=/var/www` and `SupplementaryGroups=labelwatch`
— a **per-unit** grant so the atomic directory swap can stage a sibling in
`/var/www`. That group is not held by the collector and no global group
membership was changed. `/var/www/weatherwatch` is owned by
`weatherwatch`; no Labelwatch path was chowned or chmodded.

### Disable / rollback

```bash
systemctl disable --now weatherwatch-publish.timer
systemctl disable --now weatherwatch-collector.service
rm /etc/systemd/system/weatherwatch-{collector.service,publish.service,publish.timer}
systemctl daemon-reload
```

The database, the published report and the Caddy route are all untouched by
that: `/weatherwatch` keeps serving the last published state. Removing the route is a
separate step (see the Caddy section above).

### Cursor-horizon failure posture

On restart the collector resumes at `persisted_cursor + 1` on the *same*
endpoint. If the relay no longer holds that cursor it will **not** jump to
live head pretending continuity: the first event after resume is compared
against the requested cursor, and a shortfall beyond 2s is recorded as
`gap_us` with `resume_seam=1` on that window. The window becomes
baseline-ineligible, the gap is drawn on the health strip, the previous runs
are preserved untouched, and the new process is a new run either way — restart
always creates a run boundary, never a silent continuation.

Measured 2026-08-09: a **4.41 h** cursor replayed instantly on
jetstream1.us-east, which extends M0's verified horizon (M0 confirmed 1 h and
saw ≥6 h time out within a 30 s probe). The horizon beyond that remains
unresolved.

**Known gap, not yet handled:** if the relay accepts the connection but never
delivers events for an unhonourable cursor — the shape M0 saw at ≥6 h — the
collector sits connected and silent. It would not corrupt anything (no window
opens, no cursor moves, nothing is falsely recorded), but it also would not
self-recover, and `systemctl status` would read *active*. There is no CLI flag
today to start a fresh observation without hand-editing the `meta` cursor row.
Watch for it as `journalctl -u weatherwatch-collector` going quiet with no
STATS lines. Adding an explicit opt-in path is deliberately left for a future
campaign rather than improvised here.

## Publishing

```bash
./deploy/publish.sh                  # regenerate from the local DB, then publish
./deploy/publish.sh --skip-generate  # publish an already-rendered build/beef
```

Renders locally, refuses to ship anything matching a DID / `at://` URI / CID /
Bluesky handle, rsyncs to `/var/www/weatherwatch.incoming`, then swaps by
rename. A reader sees the whole old report or the whole new one.

Verified end to end: the swap ran against the live host and left the content in
place with no stray temp directories.

## Separability

The collector runs wherever you run it, writes SQLite to local disk, and never
contacts Labelwatch or this host. Only rendered HTML is pushed, by an explicit
manual command. If Labelwatch is down, collection is unaffected; if the
collector never runs again, the last published report keeps serving.

## Not a launch

Dark deployment. No link from any Labelwatch page, no navigation entry, no
sitemap or robots change, no announcement, no public documentation of the URL.
The page carries `noindex, nofollow, noarchive` in a meta tag and the route
adds a matching `X-Robots-Tag` — defensive only, not discoverability work.
