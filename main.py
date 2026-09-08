#!/usr/bin/env python3
"""Run the whole CloudBank cost + usage pipeline, in order."""
import subprocess, sys
from pathlib import Path

SCRIPTS = Path(__file__).parent / "scripts"
STEPS = [
    ("Resolving list prices from the Billing Catalog...", "pricing.py"),
    ("Measuring node-hours per pool...",                  "node_hours.py"),
    ("Measuring persistent-disk storage...",              "storage.py"),
    ("Computing daily costs by bucket...",                "costs.py"),
    ("Fetching hub users...",                             "../users.py"),
    ("Fetching Otter grading counts...",                  "../otter_standalone_use.py"),
    ("Building dashboard...",                             "build_dashboard.py"),
]
# users.py / otter_standalone_use.py need the decrypted token file and run
# from the repo root, matching how cloudbank-pilot-hub-users invokes them.
ROOT = Path(__file__).parent
if not (ROOT / "pilots.json").is_file():
    print("Decrypting pilot tokens...")
    out = subprocess.run(["sops", "--decrypt", "enc-pilots.json"],
                         capture_output=True, text=True, cwd=ROOT)
    if out.returncode == 0:
        (ROOT / "pilots.json").write_text(out.stdout)
    else:
        print("  !! sops decrypt failed; usage steps will be skipped")

failures = []
for label, script in STEPS:
    path = SCRIPTS / script
    if not path.is_file():
        print(f"\n== SKIP {script} (not written yet)"); continue
    print(f"\n{'='*60}\n  {label}\n{'='*60}")
    env = {**__import__("os").environ, "PYTHONPATH": str(SCRIPTS)}
    if subprocess.run([sys.executable, str(path)], env=env).returncode != 0:
        failures.append(script)
        print(f"  !! {script} failed -- continuing so partial data still lands")
print("\nDone." if not failures else f"\nDone with failures: {failures}")
sys.exit(1 if failures else 0)
