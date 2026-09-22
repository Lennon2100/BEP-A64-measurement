#!/usr/bin/env python3
"""Combine completed strategy runs into cost and discovery tables.

The cost curve is derived from each batch's `probes.csv` (parsed rows are in
manifest order and carry `is_observed_positive`); no separate ledger file is
stored.
"""

import argparse
import csv
import gzip
import io
import json
import tarfile
from pathlib import Path


def planned_probes(method_dir):
    """Yield `is_observed_positive` for each planned target, in send order."""
    for archive in sorted(method_dir.glob("batch-*.tar.gz")):
        with tarfile.open(archive, "r:gz") as bundle:
            with bundle.extractfile("probes.csv") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                for row in reader:
                    yield row["is_observed_positive"] == "1"


def cost_curve(method_dir, summary, interval):
    method = summary["method"]
    probe = 0
    positives = 0
    for positive in planned_probes(method_dir):
        probe += 1
        if positive:
            positives += 1
        if probe % interval == 0:
            yield {"method": method, "probe_cost": probe, "distinct_new_positive_c64": positives}
    if probe % interval:
        yield {"method": method, "probe_cost": probe, "distinct_new_positive_c64": positives}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formal_config")
    parser.add_argument("--curve-interval", type=int, default=10000)
    args = parser.parse_args()
    if args.curve_interval <= 0:
        parser.error("--curve-interval must be positive")
    config_path = Path(args.formal_config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = Path(config["output_root"])
    if not root.is_absolute():
        root = config_path.parent / root

    summaries = []
    for method in config["strategies"]:
        summaries.append(json.loads((root / method / "summary.json").read_text(encoding="utf-8")))

    comparison_path = root / "comparison.csv"
    curve_path = root / "cost-discovery-curve.csv.gz"
    if comparison_path.exists() or curve_path.exists():
        raise FileExistsError("refusing to overwrite formal comparison")

    fields = (
        "method", "budget_total", "formal_sent", "budget_exhausted",
        "distinct_new_positive_c64", "distinct_last_hop_router_addresses",
        "probes_per_new_discovery", "stages",
    )
    with comparison_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            discoveries = summary["distinct_new_positive_c64"]
            writer.writerow({
                **{key: summary[key] for key in fields if key != "probes_per_new_discovery" and key != "stages"},
                "probes_per_new_discovery": summary["formal_sent"] / discoveries if discoveries else "",
                "stages": json.dumps(summary["stages"], sort_keys=True),
            })

    with gzip.open(curve_path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("method", "probe_cost", "distinct_new_positive_c64"))
        writer.writeheader()
        for summary in summaries:
            writer.writerows(cost_curve(root / summary["method"], summary, args.curve_interval))
    print(comparison_path)
    print(curve_path)


if __name__ == "__main__":
    main()
