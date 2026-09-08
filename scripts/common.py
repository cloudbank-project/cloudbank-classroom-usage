"""Shared helpers: paths, config, Prometheus, and CSV upsert.

Every script in this repo writes into data/ via `upsert`, which merges on a key
and keeps the historical record even when a source stops answering. That is the
whole point of committing data/ -- see the README.
"""

import base64
import datetime
import json
import os
import subprocess
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

import pandas as pd
import yaml

PT = zoneinfo.ZoneInfo("America/Los_Angeles")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)

PROM_URL = "https://prometheus.cloudbank.2i2c.cloud"
PROJECT = "cb-1003-1696"

# Where the Prometheus basic-auth secret lives. Overridable so CI can point
# somewhere else (or supply PROM_USER / PROM_PASS directly).
INFRA_REPO = Path(
    os.environ.get("INFRA_REPO", Path.home() / "Documents/cloudbank/infrastructure")
)
PROM_SECRET = INFRA_REPO / "config/clusters/cloudbank/enc-support.secret.values.yaml"


def load_pools():
    return yaml.safe_load((CONFIG_DIR / "pools.yaml").read_text())


def bucket_of(pool_name, cfg=None):
    cfg = cfg or load_pools()
    entry = cfg["pools"].get(pool_name)
    # An unknown pool is far more likely to be a new notebook pool than a new
    # always-on one, so default to cpu rather than silently inflating "base".
    return entry["bucket"] if entry else "cpu"


# --------------------------------------------------------------- Prometheus


def prom_auth():
    """(user, password) for the CloudBank Prometheus.

    Env wins so CI can inject it; otherwise decrypt the infrastructure repo's
    sops secret. gcloud access tokens do NOT work here -- the endpoint wants
    basic auth and 401s otherwise.
    """
    user, password = os.environ.get("PROM_USER"), os.environ.get("PROM_PASS")
    if user and password:
        return user, password
    if not PROM_SECRET.is_file():
        raise SystemExit(
            f"No Prometheus credentials.\n"
            f"  Set PROM_USER / PROM_PASS, or make {PROM_SECRET} readable "
            f"(clone 2i2c-org/infrastructure, or set INFRA_REPO)."
        )
    out = subprocess.run(
        ["sops", "-d", str(PROM_SECRET)], capture_output=True, text=True, check=True
    ).stdout
    secret = yaml.safe_load(out)["prometheusAuthSecret"]
    return secret["username"], secret["password"]


def _prom_get(path, params):
    user, password = prom_auth()
    url = f"{PROM_URL}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {payload}")
    return payload["data"]["result"]


def query_range(query, start, end, step):
    """Range query. `start`/`end` are tz-aware datetimes, `step` in seconds.

    ALWAYS pass a start anchored to Pacific midnight for per-day work. Buckets
    anchor to `start`, not to midnight, so an unanchored "daily" series is
    offset by whatever time you happened to run it -- that bug once attributed
    Thursday's peak to Friday.
    """
    return _prom_get(
        "/api/v1/query_range",
        {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        },
    )


def midnight_pt(days_ago=0):
    now = datetime.datetime.now(PT)
    return (now - datetime.timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# -------------------------------------------------------------------- upsert


def upsert(filename, df, key):
    """Merge `df` into data/<filename>, replacing rows that share `key`.

    Existing rows whose key is absent from `df` are preserved -- that is what
    keeps history alive when a source disappears or a backfill is partial.
    """
    path = DATA_DIR / filename
    key = [key] if isinstance(key, str) else list(key)
    if path.is_file():
        old = pd.read_csv(path)
        combined = pd.concat([old, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=key, keep="last")
    else:
        combined = df
    combined = combined.sort_values(key).reset_index(drop=True)
    combined.to_csv(path, index=False)
    print(f"  {filename}: {len(df)} row(s) in, {len(combined)} total")
    return combined


def read_data(filename):
    path = DATA_DIR / filename
    return pd.read_csv(path) if path.is_file() else pd.DataFrame()
