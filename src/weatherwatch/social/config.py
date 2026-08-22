"""Activation config for the edge sink, and the receipt that says what it did.

Two rules shape this module.

**Off unless explicitly configured.** There is no "auto", no inference from
the presence of a database file, and no default that becomes true when an
environment variable is merely *set*. `WW_SOCIAL_EDGES` must hold an
affirmative value. Anything else -- unset, empty, `0`, `no`, garbage -- is off,
and a garbage value is reported as garbage rather than quietly treated as off.

**Both states leave a receipt.** A configuration flag that only records itself
when it is on is not a receipt; it is an advertisement. `receipt()` returns the
same shape either way, and the collector writes it on every run, so "was the
sink on during this run?" is answerable from the database rather than from
whoever remembers.

The receipt deliberately carries no filesystem path in its published form: the
report renders `public_receipt()`, which says what was retained and for how
long, not where on which host it landed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from .. import timeutil
from .edges import TRACKED_ALIASES
from .envelope import receipt_hash
from .store import DEFAULT_EDGE_DB_PATH

ENV_ENABLED = "WW_SOCIAL_EDGES"
ENV_DB = "WW_SOCIAL_DB"
ENV_COLLECTIONS = "WW_SOCIAL_COLLECTIONS"
ENV_RETENTION = "WW_SOCIAL_RETENTION"

#: Where the collector records what it did. Read by the report so
#: the published page states the retention posture of the run that
#: produced it, rather than the reader having to trust a claim.
RECEIPT_META_KEY = "social_sink_receipt"
RECEIPT_FILENAME = "social_sink_receipt.json"

#: Values that turn the sink on. Everything else leaves it off.
TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
FALSEY = frozenset({"", "0", "false", "no", "off", "disabled"})

#: Boundary formation, not engagement. Blocks/follows/list membership run at
#: ~5/s, ~26/s and low single digits respectively; likes and reposts run at
#: ~216/s and ~34/s as observed, so they are opt-in on top of an opt-in.
DEFAULT_COLLECTIONS = ("block", "follow", "listitem")
DEFAULT_RETENTION = "3d"


class ConfigError(ValueError):
    """Raised on a configuration that cannot be honoured as written."""


@dataclass(frozen=True)
class SocialConfig:
    enabled: bool = False
    db_path: str = str(DEFAULT_EDGE_DB_PATH)
    collections: tuple[str, ...] = DEFAULT_COLLECTIONS
    retention: str = DEFAULT_RETENTION
    batch_rows: int = 2_000
    #: Where the decision came from, for the receipt: "default" | "env" | "cli".
    source: str = "default"

    @property
    def retention_us(self) -> int | None:
        if not self.retention:
            return None
        return int(timeutil.parse_duration(self.retention) * 1_000_000)

    @property
    def collection_set(self) -> frozenset[str] | None:
        return frozenset(self.collections) if self.collections else None

    @property
    def config_hash(self) -> str:
        return receipt_hash({
            "enabled": self.enabled,
            "collections": sorted(self.collections),
            "retention": self.retention,
            "batch_rows": self.batch_rows,
        })

    def validate(self) -> "SocialConfig":
        unknown = sorted(set(self.collections) - set(TRACKED_ALIASES))
        if unknown:
            raise ConfigError(
                f"unknown collections {unknown}; tracked: "
                f"{sorted(TRACKED_ALIASES)}"
            )
        if self.retention:
            try:
                timeutil.parse_duration(self.retention)
            except Exception as e:
                raise ConfigError(f"bad retention {self.retention!r}: {e}") from e
        return self

    # -- receipts ----------------------------------------------------------

    def receipt(self, run_id: str = "", at: str | None = None) -> dict:
        """Full local receipt. Includes the path, so it is not for publishing."""
        r = self.public_receipt(run_id, at)
        r["db_path"] = self.db_path if self.enabled else None
        return r

    def public_receipt(self, run_id: str = "", at: str | None = None) -> dict:
        """What may be shown to a reader: state, scope, horizon. No path."""
        return {
            "enabled": self.enabled,
            "collections": sorted(self.collections) if self.enabled else [],
            "retention": self.retention if self.enabled else None,
            "config_source": self.source,
            "config_hash": self.config_hash,
            "run_id": run_id,
            "recorded_at": at or timeutil.now_iso(),
        }


def _parse_bool(raw: str | None, var: str) -> bool:
    if raw is None:
        return False
    v = raw.strip().lower()
    if v in TRUTHY:
        return True
    if v in FALSEY:
        return False
    raise ConfigError(
        f"{var}={raw!r} is neither affirmative nor negative. Refusing to "
        f"guess: an ambiguous value here decides whether identity is "
        f"retained. Use one of {sorted(TRUTHY)} or {sorted(FALSEY - {''})}."
    )


def from_env(env: dict | None = None) -> SocialConfig:
    """Read activation from the environment. Absent means off."""
    env = os.environ if env is None else env
    enabled = _parse_bool(env.get(ENV_ENABLED), ENV_ENABLED)
    cols_raw = env.get(ENV_COLLECTIONS)
    collections = (
        tuple(c.strip() for c in cols_raw.split(",") if c.strip())
        if cols_raw is not None else DEFAULT_COLLECTIONS
    )
    return SocialConfig(
        enabled=enabled,
        db_path=env.get(ENV_DB) or str(DEFAULT_EDGE_DB_PATH),
        collections=collections,
        retention=env.get(ENV_RETENTION, DEFAULT_RETENTION),
        source="env" if any(k in env for k in
                            (ENV_ENABLED, ENV_DB, ENV_COLLECTIONS,
                             ENV_RETENTION)) else "default",
    ).validate()


def from_args(args, env: dict | None = None) -> SocialConfig:
    """CLI over environment. A flag beats an env var; neither defaults to on."""
    cfg = from_env(env)
    changed = {}
    if getattr(args, "social_edges", False):
        changed["enabled"] = True
    if getattr(args, "social_db", None):
        changed["db_path"] = args.social_db
    if getattr(args, "social_collections", None) is not None:
        changed["collections"] = tuple(
            c.strip() for c in args.social_collections.split(",") if c.strip())
    if getattr(args, "social_retention", None) is not None:
        changed["retention"] = args.social_retention
    if not changed:
        return cfg
    return replace(cfg, source="cli", **changed).validate()
