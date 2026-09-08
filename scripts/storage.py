"""Measure persistent-disk storage -> data/storage.csv.

Storage is the sleeper line in CloudBank's bill and the weekly-costs notebook
omits it entirely. The home-directory disks alone run to thousands of GB --
more per day than the CPU pool on a quiet day, and comparable to the GPU pool.
Any "what does CloudBank cost" answer that leaves this out is wrong low.

Disks are billed by provisioned capacity, not usage, so this is one of the few
figures here that is genuinely exact rather than modeled -- the only modeling
is the list price.

Boot disks attached to nodes are NOT counted here: they are already priced into
each pool's $/node-hour in pricing.py. Counting them again would double-bill.
"""

import collections
import datetime
import json
import subprocess

import pandas as pd

from common import PT, PROJECT, load_pools, read_data, upsert

HOURS_PER_MONTH = 730

# Disks whose name matches a node boot disk are excluded -- see module docstring.
BOOT_DISK_PREFIXES = ("gke-cb-cluster-", "gke-oss-cluster-")


def list_disks():
    out = subprocess.run(
        [
            "gcloud", "compute", "disks", "list",
            f"--project={PROJECT}",
            "--format=json(name,sizeGb,type,zone,users,labels)",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def classify(disk):
    """Bucket a disk by what it is for."""
    name = disk["name"]
    if name.startswith(BOOT_DISK_PREFIXES):
        return "node-boot"
    if "hub-nfs-homedirs" in name:
        return "home-dirs"
    return "other"


def main():
    disks = list_disks()
    print(f"{len(disks)} disks in {PROJECT}")

    pricing = read_data("pricing.csv")
    if pricing.empty:
        raise SystemExit("Run scripts/pricing.py first -- no data/pricing.csv.")
    # Recover per-GB-hour rates from pricing.csv so storage stays on the same
    # catalog prices as compute: disk_usd_hr = disk_gb * per_gb_hour, so invert
    # it using each pool's configured disk size.
    rate = {}
    cfg = load_pools()
    for _, r in pricing.iterrows():
        spec = cfg["pools"].get(r["pool"])
        if not spec or not spec.get("disk_gb"):
            continue
        dt = spec.get("disk_type", "pd-balanced")
        if dt not in rate:
            rate[dt] = float(r["disk_usd_hr"]) / float(spec["disk_gb"])

    totals = collections.defaultdict(lambda: {"gb": 0, "count": 0, "usd_hr": 0.0})
    unpriced = set()
    for d in disks:
        kind = classify(d)
        gb = int(d.get("sizeGb", 0))
        dtype = d.get("type", "").rsplit("/", 1)[-1]
        per_gb_hr = rate.get(dtype)
        if per_gb_hr is None:
            unpriced.add(dtype)
            per_gb_hr = rate.get("pd-balanced", 0.10 / HOURS_PER_MONTH)
        totals[kind]["gb"] += gb
        totals[kind]["count"] += 1
        totals[kind]["usd_hr"] += gb * per_gb_hr

    if unpriced:
        print(f"  NOTE: no catalog rate for {sorted(unpriced)}, priced as pd-balanced")

    today = datetime.datetime.now(PT).date().isoformat()
    rows = []
    for kind, t in sorted(totals.items()):
        rows.append(
            {
                "date": today,
                "category": kind,
                "disk_count": t["count"],
                "total_gb": t["gb"],
                "usd_per_hour": round(t["usd_hr"], 6),
                "usd_per_day": round(t["usd_hr"] * 24, 4),
                "usd_per_month": round(t["usd_hr"] * HOURS_PER_MONTH, 2),
                # node-boot is already inside each pool's $/node-hour
                "counted_in_total": kind != "node-boot",
            }
        )

    df = pd.DataFrame(rows)
    upsert("storage.csv", df, key=["date", "category"])

    print()
    print(f"{'category':<12}{'disks':>6}{'GB':>10}{'$/day':>10}{'$/month':>10}   counted")
    for r in rows:
        print(
            f"  {r['category']:<10}{r['disk_count']:>6}{r['total_gb']:>10,}"
            f"{r['usd_per_day']:>10.2f}{r['usd_per_month']:>10.2f}"
            f"   {'yes' if r['counted_in_total'] else 'no (in node rate)'}"
        )


if __name__ == "__main__":
    main()
