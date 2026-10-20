# P4 standalone deployment reconciliation

Source monitoring implementation: ad9ee32. The recorded production source 9daa4dc
had diverged from the testbed's portable publication source ec9d0cf. This source
reconciliation restores ec9d0cf's existing sysusers/tmpfiles and own-webroot rsync
publication path, while retaining the later production memory/timeout/priority
bounds and timer cadence. It removes the borrowed Labelwatch supplementary group
and fixed production URL; optional site identity comes from publish.env. The
collector's own state tree now includes its metrics output directory. No live
host, account, publication target or permissions were changed by this commit.

The testbed consumes .ops/service.toml from the pinned application source and
renders its contract digest. Its package update is keyed to the actual wheel set,
and a changed package/unit restarts its collector. The existing node_exporter
reads only a link to the application's ordinary .prom file. There is no new
exporter daemon or separate inventory.

Validation: publish.sh shell syntax passed; the functional Weatherwatch suite and
scheduled testbed proof are recorded in the Cartography P4 integration receipt.
This is not a publication/deployment receipt. Newer resource bounds were preserved
by source comparison; full cold-install qualification is not claimed.

Formalization consideration: root owns the bounded claim that local publication
needs write access only to its own declared target and monitoring output stays
inside application-owned state. The exact accepted portable source ec9d0cf and
source diff suffice for this reuse; existing publication tests and the P4 consumer
proof cover affected behavior. Per-file rsync replacement is not a whole-tree
atomic snapshot. No rollback of an externally visible publication is claimed.
