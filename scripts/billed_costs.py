"""Pull what CloudBank was actually billed -> data/billed_costs.csv, data/billed_by_hub.csv.

Source is our own copy of the CloudBank billing export, in cb-1003-1696:

    cb-1003-1696.billing_export.gcp_billing_export_v1

CloudBank shares the real export (StrategicBlue billing account
013B78-C37939-963D54) with Sean alone, through a row access policy limited to
project.id = cb-1003-1696:

    cloudbank-project-admin.BillingData.gcp_billing_export_v1_013B78_C37939_963D54

The "billing-export-copy" scheduled query runs daily as Sean and re-copies the
last 3 export days into ours, so this script (and CI) only needs dataViewer on
cb-1003-1696:billing_export and bigquery.jobUser on cb-1003-1696.
partition_date in the copy is the source's _PARTITIONTIME.

This sits next to the modeled costs.py, it does not replace it:
  - the export lags 1-2 days, so the model is the only figure for yesterday
  - late rows keep landing for days, so every run re-pulls the last WINDOW days
  - model vs billed is how the model's gaps get found

Buckets match costs.py (base / cpu / gpu), taken from each row's node-pool label.
Rows with no node pool (disks, snapshots, logging, networking, cluster fees)
are base. Days are Pacific, like everything else here.

    python scripts/billed_costs.py                     # last WINDOW days
    python scripts/billed_costs.py --since 2026-08-25  # backfill
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

from common import PT, PROJECT, bucket_of, load_pools, upsert

TABLE = f"{PROJECT}.billing_export.gcp_billing_export_v1"
WINDOW = 10
MAX_BYTES = 50 * 10**9

QUERY = """
SELECT
  DATE(usage_start_time, 'America/Los_Angeles') AS date,
  IFNULL((SELECT value FROM UNNEST(labels) WHERE key = 'goog-k8s-node-pool-name'), '') AS pool,
  IFNULL((SELECT value FROM UNNEST(labels) WHERE key = 'k8s-namespace'), '') AS namespace,
  service.description AS service,
  sku.description AS sku,
  SUM(cost) AS gross,
  SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
  MAX(export_time) AS exported
FROM `{table}`
WHERE partition_date >= DATE_SUB('{since}', INTERVAL 1 DAY)
  AND DATE(usage_start_time, 'America/Los_Angeles') >= '{since}'
GROUP BY 1, 2, 3, 4, 5
"""


def access_token():
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _call(url, token, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def run_query(sql):
    """Rows of a standard-SQL query as a list of dicts (paged)."""
    token = access_token()
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries"
    resp = _call(base, token, {
        "query": sql,
        "useLegacySql": False,
        "maximumBytesBilled": str(MAX_BYTES),
        "timeoutMs": 60000,
    })
    job = resp["jobReference"]["jobId"]
    loc = resp["jobReference"].get("location", "US")
    while not resp.get("jobComplete"):
        time.sleep(3)
        resp = _call(f"{base}/{job}?location={loc}&timeoutMs=60000", token)

    fields = [f["name"] for f in resp["schema"]["fields"]]
    rows = []
    while True:
        rows += [dict(zip(fields, (c["v"] for c in r["f"]))) for r in resp.get("rows", [])]
        page = resp.get("pageToken")
        if not page:
            return rows
        resp = _call(f"{base}/{job}?location={loc}&pageToken={page}", token)


def category(service, sku):
    """What the money bought, coarse enough to line up with the model."""
    s = sku.lower()
    if service == "Compute Engine":
        if "gpu" in s or "nvidia" in s:
            return "gpu"
        if "snapshot" in s:
            return "snapshots"
        if "pd capacity" in s or "storage pd" in s:
            return "disks"
        if "data transfer" in s or "ip charge" in s or "load balanc" in s or "forwarding rule" in s:
            return "network"
        return "vm"
    return {
        "Kubernetes Engine": "gke-fee",
        "Cloud Logging": "logging",
        "Cloud Monitoring": "monitoring",
        "Networking": "network",
        "Cloud Filestore": "filestore",
    }.get(service, "other")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="first Pacific day to pull (YYYY-MM-DD)")
    args = ap.parse_args()
    since = args.since or str(datetime.datetime.now(PT).date() - datetime.timedelta(days=WINDOW))

    try:
        rows = run_query(QUERY.format(table=TABLE, since=since))
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            # Expected until this identity is granted read access -- the model
            # still runs, so don't fail the whole nightly over it.
            print(f"  SKIP: no read access to {TABLE} (HTTP {e.code}).")
            print(f"  Needs dataViewer on {PROJECT}:billing_export and bigquery.jobUser")
            print(f"  on {PROJECT}.")
            return
        raise
    if not rows:
        print("  WARNING: query returned no rows -- is the billing-export-copy")
        print("  scheduled query running?")
        return

    df = pd.DataFrame(rows)
    for c in ("gross", "credits"):
        df[c] = df[c].astype(float)
    df["net"] = df["gross"] + df["credits"]
    df["exported"] = pd.to_datetime(df["exported"].astype(float), unit="s", utc=True)

    cfg = load_pools()
    df["bucket"] = df["pool"].map(lambda p: bucket_of(p, cfg) if p else "base")
    df["category"] = [category(s, k) for s, k in zip(df["service"], df["sku"])]

    by_cat = (
        df.groupby(["date", "bucket", "category"])
        .agg(gross=("gross", "sum"), credits=("credits", "sum"), net=("net", "sum"),
             exported=("exported", "max"))
        .reset_index()
    )
    by_cat["exported"] = by_cat["exported"].dt.strftime("%Y-%m-%dT%H:%MZ")
    upsert("billed_costs.csv", by_cat.round(4), ["date", "bucket", "category"])

    # Per-hub cost exists only from 2026-09-25, when GKE cost allocation was
    # enabled on cb-cluster. kube:unallocated is idle node capacity.
    hubs = df[df["namespace"] != ""]
    if not hubs.empty:
        by_hub = hubs.groupby(["date", "namespace"])["net"].sum().reset_index()
        upsert("billed_by_hub.csv", by_hub.round(4), ["date", "namespace"])

    newest = df["exported"].max()
    lag = datetime.datetime.now(datetime.timezone.utc) - newest
    print(f"  newest export row {newest:%Y-%m-%d %H:%M}Z ({lag.total_seconds()/3600:.0f}h ago)")
    if lag > datetime.timedelta(hours=48):
        print("  WARNING: billing export is more than 48h stale")


if __name__ == "__main__":
    sys.exit(main())
