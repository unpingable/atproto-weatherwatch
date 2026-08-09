# Deploy — live (dark)

Status as of 2026-08-09:

| | |
|---|---|
| `/var/www/weatherwatch-beef` on the web host | created, populated |
| `deploy/publish.sh` (render → privacy gate → atomic swap) | working, verified under load |
| Caddy route for `/beef` | **applied**, scoped to `/beef` and `/beef/*` only |
| `https://labelwatch.neutral.zone/beef` | **200** |
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
  and `stechometer`. `/beef` uses exactly this pattern — no new service, no
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
(the original form) matched anything *starting* with `/beef`, so `/beefsteak`
and `/beefy.html` were entering the Weatherwatch namespace and being answered
by its handler rather than Labelwatch's. `handle_path` accepts only one
matcher, hence the named matcher plus an explicit `uri strip_prefix`, which
keeps bare `/beef` answering 200 rather than redirecting.

Why this is additive rather than a behaviour change: `/beef` returned 404
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
curl -sS -o /dev/null -w '%{http_code}\n' https://labelwatch.neutral.zone/beef/
curl -sS -o /dev/null -w '%{http_code}\n' https://labelwatch.neutral.zone/        # must stay 200
curl -sS -o /dev/null -w '%{http_code}\n' https://stechometer.neutral.zone/       # must stay 200
```

### To roll back

```bash
$SSH 'cp -a /home/jbeck/atproto/Caddyfile.bak.<stamp> /home/jbeck/atproto/Caddyfile \
      && docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile'
```

Removing `/var/www/weatherwatch-beef` is optional; while no route points at it
it serves nothing.

## Witness declaration — gap noted, not improvised

Per "declare before you disturb", a Caddy reload on this host should be
declared to NQ first. NQ has no CLI or declaration surface **on this host** —
it is proxied from elsewhere via `172.17.0.1:9848` — so there was nothing to
declare to from here. Following the doctrine, no suppression was improvised
and the gap is recorded here instead. A graceful `caddy reload` is a config
swap with no connection drop, and Labelwatch's own witness requirements
already class config reloads as routine disturbances.

## Publishing

```bash
./deploy/publish.sh                  # regenerate from the local DB, then publish
./deploy/publish.sh --skip-generate  # publish an already-rendered build/beef
```

Renders locally, refuses to ship anything matching a DID / `at://` URI / CID /
Bluesky handle, rsyncs to `/var/www/weatherwatch-beef.incoming`, then swaps by
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
