# Deployment notes (public-safe)

Weatherwatch is published as static files. The collector and publisher are
separate processes joined only by a local SQLite database; there is no public
application server, API, or read endpoint in this repository.

This document intentionally contains no credentials, private host paths,
operator identities, SSH targets, backup names, or instructions that confer
deployment authority. Repository paths and service-unit paths are examples
that must be adapted by an authorised operator.

## Current public shape

| component | posture |
|---|---|
| collector | one named Jetstream endpoint; local SQLite |
| field sealer | one content-addressed observation per window; adds no retention |
| report | static observatory, permanent findings, `summary.json`, and `social.json` |
| publisher | render → privacy gate → atomic directory swap |
| canonical site | <https://weatherwatch.neutral.zone/> |
| crawling | `noindex, nofollow, noarchive` in HTML and web-tier header |

The page opens with the latest published finding, then compact current weather
from the named observer, explicit currentness/coverage status, recent findings,
and a folded receipts deck holding observation status, primitive rates, ratios,
observation health, social episodes and run history. Findings have permanent
pages under `findings/` with versioned aggregate JSON receipts. A finding is a
historical publication, not a substitute for current observation state. The
conditions block is read from the sealed field archive; it is not computed at
publish time.

The former `/weatherwatch` path and `/beef` alias are redirects to the
canonical host. Redirect configuration belongs to the serving environment;
the repository does not embed access credentials or the live shared-proxy
configuration.

## Repository deployment artifacts

- `deploy/systemd/weatherwatch-collector.service` supervises collection.
- `deploy/systemd/weatherwatch-field.service` seals social-weather field
  observations; `.timer` runs it hourly. It retains nothing new — the field is
  derived from the identity-free minute counters the collector already writes
  — and it renders no page. **Without it the published page reports
  `Station offline`**, correctly and permanently, because the conditions block
  reads the archive this unit writes.
- `deploy/systemd/weatherwatch-publish.service` renders and publishes once.
  It must be given `WW_SOCIAL_DB`; unset, both the social section and the
  conditions block degrade to unavailable.
- `deploy/systemd/weatherwatch-publish.timer` runs the publisher every five
  minutes.
- `deploy/publish.sh` implements local or explicit remote publication.
- `weatherwatch publication-gate` is the shared deterministic candidate gate
  used by both `publish.sh` and `weatherwatch status`. Passing means only that
  the local candidate is structurally complete and privacy-clean; it grants no
  publication authority and does not prove the target changed.
- `deploy/beef-route.caddy` documents the old path-matcher footgun; it is an
  illustrative fragment, not a complete live configuration.

Review unit paths, user/group names, resource limits, the endpoint, and the
webroot for the target environment before installation. Validate changes in a
staging environment when the web tier is shared with other sites.

## Separation and privilege boundaries

Continuous collection is a deployment choice, not a semantic requirement.
`weatherwatch collect --duration 30m` produces a valid bounded observation.
Running continuously only keeps baselines contiguous and makes cursor replay
recovery machinery rather than the normal mode.

The committed units express these invariants:

- collector and publisher do not require or order one another;
- neither service runs as root;
- the collector can write only its state directory;
- only the publisher can write the static webroot;
- both use `ProtectSystem=strict`, `ProtectHome=true`, no privilege escalation,
  and an empty capability bounding set;
- the publisher uses the same tested render/privacy/swap path as manual runs.

The example install uses:

| purpose | example path |
|---|---|
| code and virtual environment | `/opt/weatherwatch` |
| aggregate database | `/var/lib/weatherwatch/weatherwatch.sqlite` |
| local edge database | `/var/lib/weatherwatch/social.sqlite` |
| rendered staging tree | `/var/lib/weatherwatch/build/report` |
| static target | `/var/www/weatherwatch` |

These are service paths, not credentials. A deployment may choose different
locations if it preserves local-disk SQLite and the privilege split.

## Edge custody receipt

The edge sink is off by default in code. The current deployment explicitly
enables the narrow `block,listitem` set with 24-hour retention. That choice is
not encoded as a repository default and must remain an explicit local
configuration.

The collector records a public-safe receipt (enabled state, collection aliases,
retention, and configuration hash) in the aggregate database. Filesystem paths
and identities are excluded from the published receipt. Exact edge rows and
local detector envelopes never enter the static output.

Public aggregate episodes are additionally gated by `social/projection.py`:
at least ten distinct locally observed actor DIDs must have performed the same
action during the period; for a rate *excess*, no single actor may have
emitted as many events as the departure itself; output times are coarsened to
UTC hours; and exact counts/statistics/stable episode IDs are removed. Missing
or uncomputable support suppresses the episode. This is provisional disclosure
resistance, not anonymity — `src/weatherwatch/social/BOUNDARIES.md` states
what each gate does and does not establish, including the adversarial case
that forced the second one and why the hour rounding is not the load-bearing
control.

## Publishing

Install the project and qualify the exact checkout first:

```bash
python -m pip install -e ".[dev]"
./scripts/qualify.sh
```

Local mode renders, runs the privacy gate, stages beside the target, and swaps
by rename:

```bash
WW_MODE=local \
WW_DB=/path/to/weatherwatch.sqlite \
WW_SOCIAL_DB=/path/to/social.sqlite \
WW_BUILD=/path/to/staging/report \
WW_TARGET=/path/to/static/weatherwatch \
WW_PUBLIC_URL=https://weatherwatch.example/ \
./deploy/publish.sh
```

Remote mode exists for operators whose serving host is separate. It has no
repository-supplied host or key defaults; both must be passed explicitly:

```bash
WW_MODE=remote \
WW_SSH_HOST=operator@weatherwatch-host.example \
WW_SSH_KEY=/path/to/operator-managed-key \
WW_REMOTE=/path/to/static/weatherwatch \
./deploy/publish.sh
```

`--skip-generate` publishes an already-rendered staging tree. Use it only after
the same privacy gate has inspected the tree; `publish.sh` always reruns that
gate before transfer.

The privacy gate refuses DID methods, AT URIs, CID-shaped values, Bluesky
handles, and salted actor-token shapes anywhere in the rendered directory.
The generator asserts the public projection independently before writing
`social.json`. These are tripwires, not anonymisers; the disclosure policy is
the projection boundary described above.

## Web-tier integration

Serve the generated directory as static files and add the response header:

```caddyfile
weatherwatch.example {
    root * /srv/www/weatherwatch
    header X-Robots-Tag "noindex, nofollow, noarchive"
    file_server
}
```

For legacy aliases, use exact path matchers rather than a prefix such as
`/beef*`, which also matches unrelated names like `/beefsteak`. Validate a
shared proxy configuration before reloading it, then verify the canonical site
and unrelated sites independently. The live proxy topology and reload
authority are intentionally not documented here.

## Observation and recovery semantics

The configured Jetstream endpoint is written to every run. Changing endpoints
creates a hard observation seam; a cursor from one observer is never reused on
another.

On restart, the collector requests `persisted_cursor + 1`. M0 measured
`cursor=T` as inclusive and `T+1` as exact. Counts, window health, and the
cursor commit in one SQLite transaction, so a cursor cannot advance past
durable aggregate data.

If resume lands more than two seconds beyond the requested cursor, the first
window records `gap_us` and `resume_seam=1` and becomes baseline-ineligible.
The collector never jumps silently to live head and calls the interval
complete. A relay that accepts an old cursor but sends nothing remains a known
operational limitation: it corrupts no data, but an external liveness check is
needed to detect the silent connection.

## Health and rollback

The public report places its observation interval, newest complete observation,
and freshness state near the top. Freshness uses an explicit provisional
budget of two five-minute publish intervals plus one source bucket. `partial`,
`stale`, and `unavailable` are distinct; unavailable never means calm.

To stop publication or collection, disable the corresponding timer/service in
the deployment's service manager. Stopping either leaves the other independent.
Removing service definitions does not delete the databases or last static
report. Removing data or a live route is a separate destructive operation and
is intentionally not scripted here.
