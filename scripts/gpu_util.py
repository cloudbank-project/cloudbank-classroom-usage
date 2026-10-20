"""Measure how much of the GPU pool's time the GPUs do work -> data/gpu_util.csv.

node_hours.csv says how long T4 nodes were up; this says what happened on them.
GKE already exports per-GPU metrics to Cloud Monitoring
(kubernetes.io/node/accelerator/*), one series per node, which is one T4.
They are per node, not per pod: the T4s are shared by two students, and the
per-container accelerator metrics come back empty. So each 5-minute sample of a
node's GPU is joined to Prometheus to see which GPU notebooks sat on that node,
and split evenly between them. A node with no GPU notebook on it is charged to
the "(no student)" row: that is pre-warm time and the scale-down tail.

Per (date, hub):

    node_hours      GPU node-hours attributed to the hub
    active_hours    of those, samples where the GPU did any work (duty cycle >= 1%)
    util_hours      node_hours weighted by duty cycle: GPU-fully-busy equivalent
    vram_hi_hours   samples with two students on the GPU and VRAM over 90% --
                    the shared-T4 out-of-memory risk
    long_sessions   sessions of 3h+ starting that day
    long_idle       of those, sessions with the GPU idle 80%+ of the time

Needs roles/monitoring.viewer on cb-1003-1696 (granted to cb-cost-usage
2026-10-20). Metric history starts 2026-02-05.

    python scripts/gpu_util.py                     # last WINDOW days
    python scripts/gpu_util.py --since 2026-09-01  # backfill
"""

import argparse
import collections
import datetime
import json
import subprocess
import urllib.parse
import urllib.request

import pandas as pd

from common import PROJECT, PT, midnight_pt, query_range, upsert
from hub_costs import college_names

WINDOW = 10
STEP = 300  # seconds
H = STEP / 3600
IDLE_BELOW = 1.0  # duty cycle %, 5-minute mean
LONG_SESSION_H = 3
GAP = 900  # seconds without a sample that ends a session

POD_QUERY = (
    "max by (namespace, pod, node) "
    '(kube_pod_container_resource_limits{resource="nvidia_com_gpu",container="notebook"})'
)
METRICS = ("duty_cycle", "memory_used", "memory_total")


def access_token():
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
    ).stdout.strip()


def snap(ts):
    return int(round(float(ts) / STEP) * STEP)


def occupancy(start, end):
    """{(node, ts): [namespace/pod, ...]} for every 5-minute sample."""
    occ = collections.defaultdict(list)
    t = start
    while t < end:
        t2 = min(t + datetime.timedelta(days=1), end)
        for s in query_range(POD_QUERY, t, t2, STEP):
            m = s["metric"]
            if not m.get("node"):
                continue
            for ts, _ in s["values"]:
                occ[(m["node"], snap(ts))].append(f"{m['namespace']}/{m['pod']}")
        t = t2
    return {k: sorted(set(v)) for k, v in occ.items()}


def gpu_samples(start, end):
    """{(node, ts): {metric: value}} from Cloud Monitoring, 5-minute means."""
    token = access_token()
    fmt = lambda x: x.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = collections.defaultdict(dict)
    for metric in METRICS:
        page = None
        while True:
            params = {
                "filter": f'metric.type="kubernetes.io/node/accelerator/{metric}"',
                "interval.startTime": fmt(start),
                "interval.endTime": fmt(end),
                "aggregation.alignmentPeriod": f"{STEP}s",
                "aggregation.perSeriesAligner": "ALIGN_MEAN",
                "pageSize": 100000,
            }
            if page:
                params["pageToken"] = page
            url = (f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries?"
                   + urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            for s in payload.get("timeSeries", []):
                node = s["resource"]["labels"]["node_name"]
                for p in s["points"]:
                    ts = datetime.datetime.fromisoformat(p["interval"]["endTime"].replace("Z", "+00:00"))
                    v = p["value"]
                    out[(node, snap(ts.timestamp()))][metric] = float(
                        v.get("doubleValue", v.get("int64Value", 0))
                    )
            page = payload.get("nextPageToken")
            if not page:
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="first Pacific day to compute (YYYY-MM-DD)")
    args = ap.parse_args()

    first = datetime.date.fromisoformat(args.since) if args.since else midnight_pt(WINDOW).date()
    last = midnight_pt(1).date()  # yesterday is the last complete day
    # One extra day on each side, so sessions crossing the window edge are whole.
    start = datetime.datetime.combine(first - datetime.timedelta(days=1), datetime.time(0), PT)
    end = midnight_pt(0)
    print(f"Querying {first} .. {last} PT")

    occ = occupancy(start, end)
    gpu = gpu_samples(start, end)
    print(f"  {len(gpu):,} GPU samples, {len(occ):,} occupied node samples")

    day_of = lambda ts: datetime.datetime.fromtimestamp(ts, PT).date()
    agg = collections.defaultdict(lambda: collections.defaultdict(float))
    presence = collections.defaultdict(list)  # pod -> [(ts, idle)]
    for (node, ts), v in gpu.items():
        if "duty_cycle" not in v:
            continue
        day = day_of(ts)
        util = v["duty_cycle"]
        idle = util < IDLE_BELOW
        pods = occ.get((node, ts), [])
        vram_hi = (len(pods) >= 2 and v.get("memory_total")
                   and v.get("memory_used", 0) / v["memory_total"] > 0.9)
        shares = [(p.split("/")[0], 1 / len(pods)) for p in pods] or [("(no student)", 1.0)]
        for hub, w in shares:
            a = agg[(day, hub)]
            a["node_hours"] += H * w
            a["util_hours"] += H * w * util / 100
            if not idle:
                a["active_hours"] += H * w
            if vram_hi:
                a["vram_hi_hours"] += H * w
        for p in pods:
            presence[p].append((ts, idle))

    for pod, samples in presence.items():
        samples.sort()
        runs, cur = [], [samples[0]]
        for s in samples[1:]:
            if s[0] - cur[-1][0] > GAP:
                runs.append(cur)
                cur = [s]
            else:
                cur.append(s)
        runs.append(cur)
        hub = pod.split("/")[0]
        for run in runs:
            if len(run) * H < LONG_SESSION_H:
                continue
            a = agg[(day_of(run[0][0]), hub)]
            a["long_sessions"] += 1
            if sum(idle for _, idle in run) / len(run) >= 0.8:
                a["long_idle"] += 1

    names = college_names()
    rows = [
        {
            "date": str(day),
            "hub": hub,
            "college": names.get(hub, hub),
            "node_hours": round(a["node_hours"], 3),
            "active_hours": round(a["active_hours"], 3),
            "util_hours": round(a["util_hours"], 3),
            "vram_hi_hours": round(a["vram_hi_hours"], 3),
            "long_sessions": int(a["long_sessions"]),
            "long_idle": int(a["long_idle"]),
        }
        for (day, hub), a in sorted(agg.items())
        if first <= day <= last
    ]
    if not rows:
        print("  no GPU samples in range")
        return
    df = pd.DataFrame(rows)
    upsert("gpu_util.csv", df, ["date", "hub"])

    tot = df["node_hours"].sum()
    empty = df.loc[df["hub"] == "(no student)", "node_hours"].sum()
    print(f"  {tot:.0f} GPU node-hours: {df['active_hours'].sum() / tot:.0%} with the GPU working, "
          f"{empty / tot:.0%} with no student on the node")


if __name__ == "__main__":
    main()
