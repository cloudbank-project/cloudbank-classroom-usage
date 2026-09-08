"""Measure node-hours per pool per day from Prometheus -> data/node_hours.csv.

This is the backbone of every cost figure in the repo: cost = node-hours x rate.

Two things that have burned us before and are handled here:

1. Range-query buckets anchor to `start`, NOT to midnight. An unanchored
   "daily" series is offset by whatever time you ran it, which once attributed
   Thursday's peak to Friday. We anchor to Pacific midnight and aggregate
   5-minute samples ourselves.
2. `initialNodeCount` lies (it reported 18 for core-pool while 10 were
   running). Baselines come from observed minimums here, never from config.

oss-cluster is not scraped by this Prometheus, so it gets no rows -- costs.py
models it separately and flags it "estimated".
"""

import argparse
import collections
import datetime

import pandas as pd

from common import PT, bucket_of, load_pools, midnight_pt, query_range, upsert

STEP = 300  # seconds; each sample is 1/12 of a node-hour
QUERY = "count by (label_cloud_google_com_gke_nodepool) (kube_node_labels)"


def collect(days):
    """Per (date, pool): node-hours, plus peak and mean node count."""
    end = midnight_pt(0)  # today 00:00 PT -- yesterday is the last complete day
    start = end - datetime.timedelta(days=days)
    print(f"Querying {start.date()} .. {end.date()} PT (step {STEP}s)")

    series = query_range(QUERY, start, end, STEP)
    if not series:
        print("  no data returned")
        return pd.DataFrame()

    hours = collections.defaultdict(float)
    samples = collections.defaultdict(list)
    for s in series:
        pool = s["metric"].get("label_cloud_google_com_gke_nodepool")
        if not pool:
            continue
        for ts, value in s["values"]:
            day = datetime.datetime.fromtimestamp(float(ts), PT).date()
            # Skip today: it is a partial day and would look like a crash.
            if day >= end.date():
                continue
            n = float(value)
            hours[(day, pool)] += n * STEP / 3600
            samples[(day, pool)].append(n)

    cfg = load_pools()
    rows = []
    for (day, pool), node_hours in sorted(hours.items()):
        vals = samples[(day, pool)]
        rows.append(
            {
                "date": day.isoformat(),
                "pool": pool,
                "bucket": bucket_of(pool, cfg),
                "node_hours": round(node_hours, 3),
                "peak_nodes": int(max(vals)),
                "min_nodes": int(min(vals)),
                "mean_nodes": round(sum(vals) / len(vals), 2),
                "source": "measured",
            }
        )
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--days",
        type=int,
        default=14,
        help="how many complete days back to collect (default 14). "
        "Prometheus retention is 1098 days, so a large backfill is fine.",
    )
    args = ap.parse_args()

    df = collect(args.days)
    if df.empty:
        return
    upsert("node_hours.csv", df, key=["date", "pool"])

    print()
    recent = df[df["date"] == df["date"].max()]
    print(f"Most recent complete day ({df['date'].max()}):")
    for _, r in recent.sort_values("node_hours", ascending=False).iterrows():
        print(
            f"  {r['pool']:<22}{r['bucket']:<6}{r['node_hours']:8.1f} node-h"
            f"   peak {r['peak_nodes']:>3}  min {r['min_nodes']:>3}"
        )


if __name__ == "__main__":
    main()
