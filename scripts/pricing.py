"""Resolve $/hour for each node pool from the Cloud Billing Catalog API.

Writes data/pricing.csv (one row per pool, upserted by date) so every historical
cost figure records the price that was actually in force when it was computed --
rather than silently re-pricing the past when Google changes a rate.

LIST PRICE ONLY. Sustained-use discounts are not modeled (they would lower the
real bill); egress, load balancers, logging and Artifact Registry are not
modeled at all (they raise it). Until the BigQuery billing export is live there
is no invoice to reconcile against, so treat every number here as modeled.
"""

import datetime
import json
import subprocess
import urllib.request

import pandas as pd

from common import PT, load_pools, upsert

COMPUTE_SERVICE = "services/6F81-5844-456A"  # Compute Engine
REGION = "us-central1"

# Machine types are billed as separate core + RAM SKUs, so the hourly rate is
# vcpu * core_price + gb * ram_price. This table is what each pool's machine
# type actually provides.
MACHINE_SPECS = {
    "n2-highmem-2": (2, 16),
    "n2-highmem-4": (4, 32),
    "n2-highmem-8": (8, 64),
    "n2-highmem-16": (16, 128),
    "n2-highmem-64": (64, 512),
    "n1-highmem-8": (8, 52),
    "e2-standard-8": (8, 32),
}

# Persistent disk, charged per GB-month; converted to hourly below.
# Resolved from the catalog at run time; these are the fallbacks if a SKU
# lookup fails. nb-gpu-t4 is the only pool on pd-ssd -- pricing every pool as
# pd-balanced understates the GPU node by ~$0.019/hr.
HOURS_PER_MONTH = 730
# EXACT descriptions. Substring matching is unsafe here: "Balanced PD
# Capacity" is also a substring of "Regional Balanced PD Capacity", which is
# twice the price and silently inflates every pool.
DISK_SKU_DESC = {
    "pd-balanced": "Balanced PD Capacity",
    "pd-ssd": "SSD backed PD Capacity",
    "pd-standard": "Storage PD Capacity",
}
DISK_FALLBACK_GB_MONTH = {"pd-balanced": 0.10, "pd-ssd": 0.17, "pd-standard": 0.04}


def access_token():
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def fetch_skus():
    """All Compute Engine SKUs (paged)."""
    token, skus, page = access_token(), [], None
    while True:
        url = f"https://cloudbilling.googleapis.com/v1/{COMPUTE_SERVICE}/skus?pageSize=5000"
        if page:
            url += f"&pageToken={page}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        skus.extend(payload.get("skus", []))
        page = payload.get("nextPageToken")
        if not page:
            break
    return skus


def unit_price(sku):
    """USD per unit for a SKU's first pricing tier."""
    expr = sku["pricingInfo"][0]["pricingExpression"]
    tier = expr["tieredRates"][-1]["unitPrice"]
    return int(tier.get("units", 0)) + tier.get("nanos", 0) / 1e9


def match(skus, *, family, usage_type, resource_group, exclude=("Sole Tenancy", "Commitment", "Custom")):
    """Find the on-demand SKU for a family/resource in REGION."""
    for sku in skus:
        cat = sku["category"]
        if cat.get("resourceGroup") != resource_group:
            continue
        if cat.get("usageType") != usage_type:
            continue
        if REGION not in sku.get("serviceRegions", []):
            continue
        desc = sku["description"]
        if any(word in desc for word in exclude):
            continue
        if family.lower() not in desc.lower():
            continue
        return sku
    return None


def build_price_table(skus):
    """{machine_type: $/hr} plus the GPU and disk rates."""
    prices, notes = {}, {}

    # N2/E2 bill under resourceGroup CPU + RAM. N1 does NOT -- it sits under
    # resourceGroup "N1Standard" with Core/Ram distinguished only by the
    # description, which is why a CPU/RAM-only search silently misses it.
    families = {
        "n2-highmem": ("N2 Instance", "CPU", "RAM"),
        "e2-standard": ("E2 Instance", "CPU", "RAM"),
        "n1-highmem": ("N1 Predefined Instance", "N1Standard", "N1Standard"),
    }
    resolved = {}
    for prefix, (family, core_group, ram_group) in families.items():
        core = match(
            skus, family=f"{family} Core", usage_type="OnDemand", resource_group=core_group
        )
        ram = match(
            skus, family=f"{family} Ram", usage_type="OnDemand", resource_group=ram_group
        )
        if core and ram:
            resolved[prefix] = (unit_price(core), unit_price(ram))
            notes[prefix] = f"{core['description']} | {ram['description']}"
        else:
            print(f"  WARNING: no SKU pair for {prefix} (core={bool(core)} ram={bool(ram)})")

    for machine, (vcpu, gb) in MACHINE_SPECS.items():
        prefix = machine.rsplit("-", 1)[0]
        if prefix not in resolved:
            continue
        core_p, ram_p = resolved[prefix]
        prices[machine] = round(vcpu * core_p + gb * ram_p, 6)

    gpu = match(skus, family="Tesla T4", usage_type="OnDemand", resource_group="GPU")
    if gpu:
        prices["nvidia-tesla-t4"] = round(unit_price(gpu), 6)
        notes["nvidia-tesla-t4"] = gpu["description"]

    for disk_type, desc in DISK_SKU_DESC.items():
        sku = next(
            (
                k
                for k in skus
                if k["description"] == desc
                and k["category"].get("usageType") == "OnDemand"
                and REGION in k.get("serviceRegions", [])
            ),
            None,
        )
        gb_month = unit_price(sku) if sku else DISK_FALLBACK_GB_MONTH[disk_type]
        if sku is None:
            print(f"  WARNING: no catalog SKU for {disk_type}, using fallback {gb_month}/GB-mo")
        prices[f"{disk_type}-gb-hour"] = round(gb_month / HOURS_PER_MONTH, 9)
    return prices, notes


def main():
    cfg = load_pools()
    print("Fetching Compute Engine SKUs from the Cloud Billing Catalog...")
    skus = fetch_skus()
    print(f"  {len(skus)} SKUs")
    prices, notes = build_price_table(skus)

    missing = [m for m in MACHINE_SPECS if m not in prices]
    if missing:
        print(f"  WARNING: no catalog price resolved for {missing}")

    today = datetime.datetime.now(PT).date().isoformat()
    rows = []
    for pool, spec in cfg["pools"].items():
        machine = spec["machine_type"]
        node_rate = prices.get(machine)
        if node_rate is None:
            print(f"  SKIP {pool}: unknown machine type {machine}")
            continue
        disk_type = spec.get("disk_type", "pd-balanced")
        disk_rate = spec.get("disk_gb", 0) * prices[f"{disk_type}-gb-hour"]
        accel_rate = 0.0
        if spec.get("accelerator") == "nvidia-tesla-t4":
            accel_rate = prices["nvidia-tesla-t4"] * spec.get("accelerator_count", 1)
        rows.append(
            {
                "date": today,
                "pool": pool,
                "bucket": spec["bucket"],
                "machine_type": machine,
                "disk_type": disk_type,
                "compute_usd_hr": round(node_rate, 6),
                "disk_usd_hr": round(disk_rate, 6),
                "accel_usd_hr": round(accel_rate, 6),
                "node_usd_hr": round(node_rate + disk_rate + accel_rate, 6),
            }
        )

    df = pd.DataFrame(rows)
    upsert("pricing.csv", df, key=["date", "pool"])
    print()
    print(f"{'pool':<22}{'bucket':<7}{'$/node-hr':>10}")
    for r in sorted(rows, key=lambda x: (x["bucket"], x["pool"])):
        print(f"  {r['pool']:<20}{r['bucket']:<7}{r['node_usd_hr']:>10.4f}")


if __name__ == "__main__":
    main()
