#!/usr/bin/env python3
"""Warn when this repo's pilot list has drifted from the canonical registry.

This repo keeps its OWN copy of enc-pilots.json: the CloudBank subset of
`sean-morris/cloudbank-pilot-hub-users`, re-encrypted with CloudBank's KMS key
(the Cal-ICOR split, 05b20a0). Nothing keeps the two in step, so every hub
added upstream silently goes missing here -- `csusm` was added to the registry
on 2026-09-07 and was still absent on 2026-09-24, quietly under-reporting the
fleet by one institution for two and a half weeks.

sops only encrypts the `token` field (`encrypted_regex: '^(token)$'`), so the
hub names are readable straight out of the encrypted file and this needs no KMS
access on either side -- just an unauthenticated fetch of a public file.

Never fails the pipeline: a new hub upstream is news, not a broken build. It
writes data/pilot_drift.json so the dashboard can show the warning where it
will actually be seen, and exits 0 either way.
"""

import json
import urllib.request

from common import BASE_DIR, DATA_DIR

REGISTRY_URL = (
    "https://raw.githubusercontent.com/sean-morris/"
    "cloudbank-pilot-hub-users/main/enc-pilots.json"
)
WHERE = "cloudbank"


def registry_hubs():
    """{url: name} for the CloudBank pilots in the canonical registry."""
    req = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": "cloudbank-classroom-usage"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return {p["url"]: p.get("name", p["url"]) for p in data["pilots"] if p.get("where") == WHERE}


def local_hubs():
    """{url: name} for the pilots this repo actually queries."""
    data = json.loads((BASE_DIR / "enc-pilots.json").read_text())
    return {p["url"]: p.get("name", p["url"]) for p in data["pilots"]}


def main():
    try:
        remote = registry_hubs()
    except Exception as exc:
        # An unreachable registry must not take the nightly down with it.
        print(f"  could not reach the registry ({exc}) -- skipping drift check")
        return {}

    local = local_hubs()
    missing = {u: remote[u] for u in sorted(set(remote) - set(local))}
    extra = {u: local[u] for u in sorted(set(local) - set(remote))}

    print(f"  registry: {len(remote)} CloudBank hubs · this repo: {len(local)}")
    if not missing and not extra:
        print("  in sync")
    if missing:
        print(f"  !! {len(missing)} hub(s) in the registry but NOT here -- under-reporting:")
        for url, name in missing.items():
            print(f"       {url}  ({name})")
    if extra:
        print(f"  !! {len(extra)} hub(s) here but NOT in the registry -- stale entries:")
        for url, name in extra.items():
            print(f"       {url}  ({name})")
    if missing or extra:
        print("  fix: re-sync enc-pilots.json from the registry (see README)")

    return {"registry_count": len(remote), "local_count": len(local),
            "missing": missing, "extra": extra}


if __name__ == "__main__":
    drift = main()
    (DATA_DIR / "pilot_drift.json").write_text(json.dumps(drift, indent=2) + "\n")
