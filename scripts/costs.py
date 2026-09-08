"""Join node-hours x pricing (+ storage, + cluster fees) -> data/daily_costs.csv.

Produces one row per (date, bucket) with the split Sean asked for:

    base   always-on floor -- core-pool, oss-cluster, GKE fees, storage.
           What CloudBank costs to simply exist, before any student logs in.
    cpu    student notebook pools.
    gpu    GPU notebook pools.

Everything here is MODELED: measured usage x list price. There is no invoice
to reconcile against until the BigQuery billing export is enabled on billing
account 013B78-C37939-963D54. Two known biases, in opposite directions:
  - sustained-use discounts are not modeled  -> real bill is LOWER
  - egress, load balancers, logging, Artifact Registry are not modeled
    -> real bill is HIGHER
"""

import datetime

import pandas as pd

from common import PT, load_pools, read_data, upsert


def latest_pricing():
    """Most recent rate per pool, as a {pool: $/node-hr} dict."""
    pricing = read_data("pricing.csv")
    if pricing.empty:
        raise SystemExit("Run scripts/pricing.py first -- no data/pricing.csv.")
    newest = pricing.sort_values("date").groupby("pool").tail(1)
    return dict(zip(newest["pool"], newest["node_usd_hr"]))


def storage_per_day():
    """$/day of storage that is NOT already inside a pool's node rate."""
    storage = read_data("storage.csv")
    if storage.empty:
        return 0.0
    newest = storage.sort_values("date").groupby("category").tail(1)
    counted = newest[newest["counted_in_total"].astype(str).str.lower() == "true"]
    return float(counted["usd_per_day"].sum())


def main():
    cfg = load_pools()
    rates = latest_pricing()
    node_hours = read_data("node_hours.csv")
    if node_hours.empty:
        raise SystemExit("Run scripts/node_hours.py first -- no data/node_hours.csv.")

    # --- measured pools -----------------------------------------------------
    nh = node_hours.copy()
    nh["usd"] = nh.apply(
        lambda r: round(float(r["node_hours"]) * rates.get(r["pool"], 0.0), 4), axis=1
    )
    unpriced = sorted(set(nh[nh["usd"] == 0]["pool"]) - set(rates))
    if unpriced:
        print(f"  WARNING: no rate for {unpriced} -- counted as $0")

    rows = []
    for (date, bucket), grp in nh.groupby(["date", "bucket"]):
        rows.append(
            {
                "date": date,
                "bucket": bucket,
                "node_hours": round(grp["node_hours"].sum(), 2),
                "usd": round(grp["usd"].sum(), 4),
                "basis": "measured",
            }
        )

    # --- unmeasured always-on pieces, added to base -------------------------
    # oss-cluster has no Prometheus, so it is modeled at fixed_nodes * 24h.
    # GKE charges a flat per-cluster fee beyond the free tier.
    # Storage is provisioned capacity, exact except for the list price.
    dates = sorted(nh["date"].unique())
    oss_usd_day = sum(
        spec.get("fixed_nodes", 0) * 24 * rates.get(pool, 0.0)
        for pool, spec in cfg["pools"].items()
        if not cfg["clusters"][spec["cluster"]]["prometheus"]
    )
    n_clusters = len(cfg["clusters"])
    fee_usd_day = (
        max(0, n_clusters - cfg.get("gke_free_clusters", 0))
        * cfg.get("gke_cluster_fee_usd_per_hour", 0.0)
        * 24
    )
    stor_usd_day = storage_per_day()

    for date in dates:
        for label, usd in (
            ("oss-cluster", oss_usd_day),
            ("gke-cluster-fee", fee_usd_day),
            ("storage", stor_usd_day),
        ):
            if usd:
                rows.append(
                    {
                        "date": date,
                        "bucket": "base",
                        "node_hours": 0,
                        "usd": round(usd, 4),
                        "basis": f"estimated:{label}",
                    }
                )

    df = pd.DataFrame(rows)
    upsert("daily_costs.csv", df, key=["date", "bucket", "basis"])

    # --- report -------------------------------------------------------------
    daily = df.groupby(["date", "bucket"])["usd"].sum().unstack(fill_value=0)
    for b in ("base", "cpu", "gpu"):
        if b not in daily:
            daily[b] = 0.0
    daily = daily[["base", "cpu", "gpu"]]
    daily["total"] = daily.sum(axis=1)

    print()
    print("DAILY  ($, modeled)")
    print(f"  {'date':<12}{'base':>9}{'cpu':>9}{'gpu':>9}{'total':>10}")
    for date, r in daily.tail(14).iterrows():
        print(f"  {date:<12}{r['base']:>9.2f}{r['cpu']:>9.2f}{r['gpu']:>9.2f}{r['total']:>10.2f}")

    idx = pd.to_datetime(daily.index)
    for label, freq in (("WEEKLY (week ending Sun)", "W-SUN"), ("MONTHLY", "MS")):
        agg = daily.set_index(idx).resample(freq).sum()
        print()
        print(f"{label}  ($, modeled)")
        print(f"  {'period':<12}{'base':>9}{'cpu':>9}{'gpu':>9}{'total':>10}")
        for period, r in agg.iterrows():
            print(
                f"  {period.date().isoformat():<12}{r['base']:>9.2f}"
                f"{r['cpu']:>9.2f}{r['gpu']:>9.2f}{r['total']:>10.2f}"
            )

    base_day = daily["base"].tail(7).mean()
    print()
    print(f"ALWAYS-ON BASE: ${base_day:,.2f}/day  =  ${base_day*30:,.0f}/month")
    print("  (core-pool + oss-cluster + GKE fees + storage; runs with zero students)")


if __name__ == "__main__":
    main()
