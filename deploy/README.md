# Deploy — PROPOSED, NOT APPLIED

**Nothing in this directory has been applied to any host.** It is a written
proposal for review, produced because deploying the dashboard under
`labelwatch.neutral.zone/beef` turned out to require touching shared
production infrastructure, which the M5–M7 brief said to stop and report
rather than improvise.

## Why this stopped

Deploying under that path needs three things, and the third is the problem:

1. A directory on the box for the generated static output. Routine.
2. A way to get the artifact there (rsync over SSH). Routine — but
   `DEPLOY_SSH_KEY` / `DEPLOY_HOST` are unset in this environment, so no
   credentials are available here anyway.
3. **A route for `/beef` on the Caddy instance that terminates
   `labelwatch.neutral.zone`.** Labelwatch's own docs record that
   *"Caddy is a shared proxy for 7 sites; config reloads are routine
   disturbances"* (`labelwatch/specs/gaps/gap-spec-witness-coverage-requirements.md`,
   H-4). Editing that config touches seven unrelated sites and reloads a
   proxy that a witness system is watching.

That is not obviously isolated, so it is not something to improvise.

## The trap that must be avoided

The tempting shortcut is to write the report into `/var/www/labelwatch/beef`
and skip the Caddy change. **Do not.** Labelwatch regenerates its site by
atomic directory replacement — it renames a freshly built tree over its
webroot. Anything else living inside that tree is deleted on the next
regeneration. Worse, it would couple the two systems in exactly the way the
brief forbids: the dashboard would silently vanish whenever labelwatch
published.

The output directory must be outside labelwatch's webroot.

## What is needed (for review, then execution by a human)

```bash
# 1. A directory owned by whoever runs the collector, outside labelwatch's tree
sudo mkdir -p /var/www/weatherwatch-beef
sudo chown "$COLLECTOR_USER":"$COLLECTOR_USER" /var/www/weatherwatch-beef
```

```caddyfile
# 2. Inside the existing labelwatch.neutral.zone site block.
#    THIS IS THE CHANGE THAT NEEDS A DECISION — it edits a shared proxy.
handle_path /beef/* {
    root * /var/www/weatherwatch-beef
    file_server
    header {
        X-Robots-Tag "noindex, nofollow, noarchive"
        Cache-Control "no-cache, must-revalidate"
    }
}
```

```bash
# 3. Publish. The collector writes locally; only the rendered output ships.
weatherwatch report --output /var/www/weatherwatch-beef
# or, from a workstation:
rsync -a --delete ./beef/ "$HOST:/var/www/weatherwatch-beef/"
```

Per this repo's own operating doctrine, a Caddy reload on a witnessed host is
a **declared disturbance**: declare the window to the witness before touching
it, sign the declaration, and cover the signals a reload actually disturbs.
Do not suppress alerts instead.

## Separability

The collector never talks to Labelwatch, never reads its database, and never
requires it to be healthy. It writes a SQLite file on local disk; the
dashboard is rendered from that file into a directory. If Labelwatch is down,
collection continues; if the collector is down, the last rendered report keeps
serving. That independence is deliberate and should survive any deployment
choice made here.

## Not a launch

Whatever is decided, this is a dark deployment: no link from any Labelwatch
page, no navigation entry, no sitemap, no announcement, no README pointing at
a live URL. The generated HTML carries `noindex, nofollow, noarchive` and the
proposed route sets `X-Robots-Tag` to match — defensive only, not
discoverability work.
