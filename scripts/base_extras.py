"""Measure the always-on costs that are NOT node-hours -> data/base_extras.csv.

These sit in the `base` bucket alongside core-pool and storage: they run whether
or not a single student logs in, and none of them were in the original cost
model.

Quantities here are measured from the project every run -- which matters most
for snapshots, since they accumulate daily and a hardcoded number goes stale
within a week.

UNIT PRICES ARE PUBLISHED LIST RATES, not catalog lookups. The snapshot and
load-balancing SKUs are tiered and region-named in ways the Billing Catalog
matcher cannot resolve safely (e.g. "Balanced PD Capacity" vs "Regional
Balanced PD Capacity"), so guessing wrong there is worse than pinning a
documented rate and labelling it. Confirm against the invoice once the BigQuery
billing export exists.

Two always-on costs remain UNMEASURABLE from here, and either could exceed
everything in this file:
  - internet egress (~$0.12/GB) -- every notebook and package pull
  - Cloud Logging ingestion beyond the free 50 GiB/project/month
Both need the billing export.
"""

import datetime
import json
import subprocess

import pandas as pd

from common import PT, PROJECT, upsert

HOURS_PER_MONTH = 730

# Published us-central1 list rates. See module docstring for why these are not
# catalog lookups.
SNAPSHOT_GB_MONTH = 0.026
FORWARDING_RULE_HR_FIRST5 = 0.025  # flat, covers the first five rules
FORWARDING_RULE_HR_EXTRA = 0.010   # each rule beyond five
UNATTACHED_IP_HR = 0.0075          # attached IPs are free
GCS_STANDARD_GB_MONTH = 0.020


def gcloud_json(*args):
    out = subprocess.run(
        ["gcloud", *args, f"--project={PROJECT}", "--format=json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print(f"  WARNING: `gcloud {' '.join(args)}` failed: {out.stderr.strip()[:120]}")
        return []
    return json.loads(out.stdout or "[]")


def main():
    rows = []
    today = datetime.datetime.now(PT).date().isoformat()

    def add(component, quantity, unit, usd_month, note=""):
        rows.append(
            {
                "date": today,
                "component": component,
                "quantity": quantity,
                "unit": unit,
                "usd_per_month": round(usd_month, 2),
                "usd_per_day": round(usd_month / 30, 4),
                "note": note,
            }
        )

    # --- snapshots ----------------------------------------------------------
    snaps = gcloud_json("compute", "snapshots", "list")
    gb = sum(int(s.get("storageBytes", 0)) for s in snaps) / 1e9
    add(
        "snapshots", round(gb, 1), "GB", gb * SNAPSHOT_GB_MONTH,
        f"{len(snaps)} snapshots; grows daily, check retention",
    )

    # --- load balancers -----------------------------------------------------
    rules = gcloud_json("compute", "forwarding-rules", "list")
    n = len(rules)
    lb_hr = (
        min(n, 5) / max(min(n, 5), 1) * FORWARDING_RULE_HR_FIRST5 if n else 0
    ) + max(0, n - 5) * FORWARDING_RULE_HR_EXTRA
    add("load-balancers", n, "forwarding rules", lb_hr * HOURS_PER_MONTH,
        "flat rate covers the first five rules")

    # --- unattached static IPs (attached ones are free) ---------------------
    addrs = gcloud_json("compute", "addresses", "list")
    idle = [a for a in addrs if a.get("status") != "IN_USE"]
    add("idle-static-ips", len(idle), "addresses",
        len(idle) * UNATTACHED_IP_HR * HOURS_PER_MONTH,
        f"{len(addrs)} total, {len(addrs)-len(idle)} attached and therefore free")

    # --- GCS ----------------------------------------------------------------
    buckets = subprocess.run(
        ["gsutil", "ls", "-p", PROJECT], capture_output=True, text=True
    ).stdout.split()
    gcs_gb = 0.0
    for b in buckets:
        du = subprocess.run(["gsutil", "du", "-s", b], capture_output=True, text=True).stdout
        if du.split():
            gcs_gb += int(du.split()[0]) / 1e9
    add("gcs-buckets", round(gcs_gb, 2), "GB", gcs_gb * GCS_STANDARD_GB_MONTH,
        f"{len(buckets)} buckets")

    df = pd.DataFrame(rows)
    upsert("base_extras.csv", df, key=["date", "component"])

    total = sum(r["usd_per_month"] for r in rows)
    print()
    print(f"{'component':<20}{'quantity':>12} {'unit':<18}{'$/mo':>9}{'$/day':>9}")
    for r in rows:
        print(
            f"  {r['component']:<18}{r['quantity']:>12} {r['unit']:<18}"
            f"{r['usd_per_month']:>9.2f}{r['usd_per_day']:>9.2f}"
        )
    print(f"  {'TOTAL':<18}{'':>12} {'':<18}{total:>9.2f}{total/30:>9.2f}")
    print()
    print("  NOT counted (need the billing export): internet egress, Cloud Logging.")


if __name__ == "__main__":
    main()
