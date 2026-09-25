# CloudBank Classroom Usage & Cost

Models what CloudBank costs to run, split into the three buckets that matter
operationally, and tracks how much of it is actually being used.

Modeled on [`edx-berkeley/edx-usage`](https://github.com/edx-berkeley): a
pipeline of small scripts writing committed CSVs into `data/`, plus a static
dashboard rendered into `docs/` and served from GitHub Pages.

## The three buckets

| bucket | what it is | pools |
|---|---|---|
| **base** | the always-on floor — runs whether or not a single student logs in | `core-pool`, `oss-cluster`, GKE cluster fees, persistent storage |
| **cpu** | student notebook pools | `nb-n2-highmem-4-a/b`, `-16`, `-64`, `dask-`, `test-pool` |
| **gpu** | GPU notebook pools, ~7× the hourly rate | `nb-gpu-t4` |

`base` is the number to quote when asked what CloudBank costs simply to exist.

## Everything here is MODELED

Cost is **measured node-hours × Cloud Billing Catalog list price**. There is no
invoice to reconcile against, because the BigQuery billing export has never been
enabled — `billing_export` exists in `cb-1003-1696` but holds zero tables, and
enabling it needs Billing Account Administrator on `013B78-C37939-963D54`,
which Sean does not currently have.

Two known biases, in opposite directions:

- sustained-use discounts are **not** modeled → the real bill is **lower**
- egress, load balancers, logging and Artifact Registry are **not** modeled →
  the real bill is **higher**

The dataset is US multi-region, so whenever the export is switched on it will
backfill to the start of the previous month. At that point `billing.csv` becomes
a second source to reconcile against, and these figures stop being the only
answer.

## Layout

```
config/pools.yaml      pool -> bucket, machine type, disk type/size, accelerator
main.py                runs every step in order
scripts/
  common.py            paths, config, Prometheus client, CSV upsert
  pricing.py           Billing Catalog -> data/pricing.csv     ($/node-hour)
  node_hours.py        Prometheus     -> data/node_hours.csv   (measured usage)
  storage.py           gcloud disks   -> data/storage.csv      (provisioned GB)
  costs.py             joins them     -> data/daily_costs.csv  (by bucket)
  build_dashboard.py   renders docs/index.html
data/                  committed and upserted each run, so history survives
docs/index.html        generated static dashboard (GitHub Pages)
```

`data/` is committed on purpose. Every run **upserts** rather than overwrites,
so the historical record outlives any source that stops answering.

## Setup

```bash
conda env create -f environment.yaml
conda activate cloudbank-classroom-usage
gcloud auth login          # Billing Catalog + gcloud compute disks
python main.py
```

Prometheus credentials come from the `2i2c-org/infrastructure` clone at
`~/Documents/cloudbank/infrastructure` (decrypted with `sops`). Point elsewhere
with `INFRA_REPO=/path/to/infrastructure`, or set `PROM_USER` / `PROM_PASS`
directly in CI.

Backfill further than the default 14 days with
`python scripts/node_hours.py --days 90` — Prometheus retention is 1098 days.

## Re-syncing the pilot list

`enc-pilots.json` here is **a copy**: the CloudBank subset of
[`cloudbank-pilot-hub-users`](https://github.com/sean-morris/cloudbank-pilot-hub-users),
re-encrypted with CloudBank's own KMS key when Cal-ICOR was split out. Nothing
keeps the two in step automatically, so a hub added to the registry is missing
here until someone re-syncs — `csusm` went unreported for two and a half weeks
that way.

The nightly `check_deployment_sync.py` in **cloudbank-pilot-hub-users** posts
to Slack when the two lists disagree (category: "Out of sync with
cloudbank-classroom-usage's copy"). When it does, re-sync:

```bash
sops -d ../cloudbank-pilot-hub-users/enc-pilots.json > /tmp/reg.json
python3 -c "
import json
reg = json.load(open('/tmp/reg.json'))['pilots']
cb  = sorted((p for p in reg if p.get('where') == 'cloudbank'), key=lambda p: p['url'])
json.dump({'pilots': [{'url': p['url'], 'name': p['name'], 'token': p['token']} for p in cb]},
          open('pilots.json', 'w'), indent=2)
"
sops -e pilots.json > enc-pilots.json.tmp && mv enc-pilots.json.tmp enc-pilots.json
sops -d enc-pilots.json | head -5     # round-trip check
rm -f pilots.json /tmp/reg.json       # never leave plaintext tokens around
```

The `where` field is dropped on the way in — everything here is CloudBank, and
`users.py` defaults it. Decrypting the registry needs the **cal-icor-hubs** KMS
key; re-encrypting needs **cb-1003-1696**'s, so you need both to do this.

## Gotchas worth knowing before you edit this

- **Anchor range queries to Pacific midnight.** Prometheus buckets anchor to
  `start`, not midnight; an unanchored daily series is offset by whatever time
  you ran it, which once attributed Thursday's peak to Friday.
- **`initialNodeCount` lies.** It reported 18 for `core-pool` while 10 were
  running. Baselines come from observed minimums.
- **Match disk SKUs exactly.** `"Balanced PD Capacity"` is a substring of
  `"Regional Balanced PD Capacity"`, which is double the price.
- **N1 is not under resourceGroup `CPU`/`RAM`.** It sits under `N1Standard`, so
  a CPU/RAM-only SKU search silently misses the entire GPU pool.
- **`nb-gpu-t4` is the only pool on `pd-ssd`.** Pricing every pool as
  pd-balanced understates the GPU node by ~$0.019/hour.
- **Node boot disks are already inside each pool's $/node-hour.** `storage.py`
  excludes them so they are not billed twice.
