"""Split each day's modeled CPU and GPU pool cost across hubs -> data/hub_costs.csv.

costs.py prices the pools as a whole. This divides each day's cpu and gpu
bucket between the hubs that used them:

    gpu  by GPU pod-hours (notebook containers with an nvidia.com/gpu limit)
    cpu  by notebook memory-request GB-hours, GPU pods excluded

so idle node time (pre-warm windows, partly full nodes) lands on the hubs that
used those nodes, in proportion to their use. The base bucket (core-pool,
disks, cluster fees) is not split; it is shared by every hub.

Everything here is MODELED, like costs.py. Billed per-hub numbers come from
billed_costs.py once the billing export carries namespace labels.

    python scripts/hub_costs.py                     # last WINDOW days
    python scripts/hub_costs.py --since 2026-09-01  # backfill
"""

import argparse
import collections
import datetime
import json

import pandas as pd

from common import BASE_DIR, PT, midnight_pt, query_range, read_data, upsert

WINDOW = 10
STEP = 300  # seconds

GPU_QUERY = (
    'count by (namespace) '
    '(kube_pod_container_resource_limits{resource="nvidia_com_gpu",container="notebook"})'
)
CPU_QUERY = (
    'sum by (namespace) '
    '(kube_pod_container_resource_requests{resource="memory",container="notebook"} '
    'unless on(namespace, pod) kube_pod_container_resource_limits{resource="nvidia_com_gpu"})'
)


def college_names():
    """hub slug -> institution name, from the decrypted pilot list if present."""
    path = BASE_DIR / "pilots.json"
    if not path.is_file():
        return {}
    pilots = json.loads(path.read_text()).get("pilots", [])
    return {p["url"]: p["name"] for p in pilots}


def integrate(query, start, end, scale=1.0):
    """{namespace: sum of the series over the day, in hours} for one PT day."""
    out = collections.defaultdict(float)
    for series in query_range(query, start, end, STEP):
        ns = series["metric"].get("namespace")
        out[ns] += sum(float(v) for _, v in series["values"]) * STEP / 3600 * scale
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="first Pacific day to compute (YYYY-MM-DD)")
    args = ap.parse_args()

    costs = read_data("daily_costs.csv")
    if costs.empty:
        raise SystemExit("Run scripts/costs.py first -- no data/daily_costs.csv.")
    pools = costs[costs["bucket"].isin(["cpu", "gpu"])].groupby(["date", "bucket"])["usd"].sum()

    first = (
        datetime.date.fromisoformat(args.since)
        if args.since
        else midnight_pt(WINDOW).date()
    )
    last = midnight_pt(1).date()  # yesterday is the last complete day
    names = college_names()

    rows = []
    day = first
    while day <= last:
        key = str(day)
        gpu_usd = pools.get((key, "gpu"), 0.0)
        cpu_usd = pools.get((key, "cpu"), 0.0)
        if gpu_usd or cpu_usd:
            start = datetime.datetime.combine(day, datetime.time(0), PT)
            end = start + datetime.timedelta(days=1, seconds=-STEP)
            gpu = integrate(GPU_QUERY, start, end)
            cpu = integrate(CPU_QUERY, start, end, scale=1e-9)
            g_total, c_total = sum(gpu.values()), sum(cpu.values())
            for hub in sorted(set(gpu) | set(cpu)):
                g, c = gpu.get(hub, 0.0), cpu.get(hub, 0.0)
                rows.append({
                    "date": key,
                    "hub": hub,
                    "college": names.get(hub, hub),
                    "gpu_pod_hours": round(g, 2),
                    "cpu_gb_hours": round(c, 2),
                    "gpu_usd": round(g / g_total * gpu_usd, 4) if g_total else 0.0,
                    "cpu_usd": round(c / c_total * cpu_usd, 4) if c_total else 0.0,
                })
        day += datetime.timedelta(days=1)

    if not rows:
        print("  no days with pool costs in range")
        return
    upsert("hub_costs.csv", pd.DataFrame(rows), ["date", "hub"])


if __name__ == "__main__":
    main()
