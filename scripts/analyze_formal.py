#!/usr/bin/env python3
"""Combine completed strategy ledgers into cost and discovery tables."""

import argparse
import csv
import gzip
import io
import json
import tarfile
from heapq import merge
from itertools import groupby
from pathlib import Path


def ledger_rows(method_dir):
    for archive in sorted(method_dir.glob("batch-*.tar.gz")):
        with tarfile.open(archive, "r:gz") as bundle:
            with bundle.extractfile("ledger.csv") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8", newline="") as fh:
                    yield from csv.DictReader(fh)


def cost_curve(method_dir, summary, interval):
    sent = summary["formal_sent"]
    budget = summary["budget_total"]
    checkpoints = (cost for cost, _ in groupby(merge(range(0, budget + 1, interval), (sent,))))
    iterator = iter(ledger_rows(method_dir))
    upcoming = next(iterator, None)
    positives = 0
    for ceiling in checkpoints:
        if ceiling > sent:
            break
        while upcoming is not None and int(upcoming["probe_number"]) <= ceiling:
            positives = int(upcoming["distinct_positive"])
            upcoming = next(iterator, None)
        yield {"method": summary["method"], "probe_cost": ceiling, "distinct_new_positive_c64": positives}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formal_config")
    parser.add_argument("--curve-interval", type=int, default=10000)
    args = parser.parse_args()
    if args.curve_interval <= 0:
        parser.error("--curve-interval must be positive")
    config_path = Path(args.formal_config).resolve()
    with config_path.open(encoding="utf-8") as fh:
        config = json.load(fh)
    root = Path(config["output_root"])
    if not root.is_absolute():
        root = config_path.parent / root
    summaries = []
    for method in config["strategies"]:
        with (root / method / "summary.json").open(encoding="utf-8") as fh:
            summary = json.load(fh)
        summaries.append(summary)
    comparison_path = root / "comparison.csv"
    curve_path = root / "cost-discovery-curve.csv.gz"
    if comparison_path.exists() or curve_path.exists():
        raise FileExistsError("refusing to overwrite formal comparison")
    with comparison_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("method", "budget_total", "formal_sent", "budget_exhausted", "distinct_new_positive_c64", "distinct_last_hop_router_addresses", "probes_per_new_discovery", "stages"))
        writer.writeheader()
        for summary in summaries:
            discoveries = summary["distinct_new_positive_c64"]
            writer.writerow({**{key: summary[key] for key in ("method", "budget_total", "formal_sent", "budget_exhausted", "distinct_new_positive_c64", "distinct_last_hop_router_addresses")}, "probes_per_new_discovery": summary["formal_sent"] / discoveries if discoveries else "", "stages": json.dumps(summary["stages"], sort_keys=True)})
    with gzip.open(curve_path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("method", "probe_cost", "distinct_new_positive_c64"))
        writer.writeheader()
        for summary in summaries:
            writer.writerows(cost_curve(root / summary["method"], summary, args.curve_interval))
    print(comparison_path)
    print(curve_path)


if __name__ == "__main__":
    main()
