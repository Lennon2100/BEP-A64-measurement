#!/usr/bin/env python3
"""Combine three completed formal ledgers without treating D050 as a new find."""

import argparse
import csv
import json
from pathlib import Path


METHODS = ("journal", "tnet", "subrecon")


def cost_curve(ledger_path, summary, interval):
    history = summary["historical_attributed"]
    sent = summary["formal_sent"]
    budget = summary["budget_total"]
    checkpoints = sorted(set([0, history, history + sent] + list(range(interval, budget + 1, interval))))
    with open(ledger_path, newline="", encoding="utf-8") as fh:
        iterator = iter(csv.DictReader(fh))
        upcoming = next(iterator, None)
        previous_cost = history
        positives = 0
        for ceiling in checkpoints:
            if ceiling > history + sent:
                break
            while upcoming is not None and int(upcoming["attributed_total"]) <= ceiling:
                cost = int(upcoming["attributed_total"])
                found = int(upcoming["distinct_positive"])
                if cost != previous_cost + 1 or found < positives or found > positives + 1:
                    raise ValueError(f"non-monotone formal ledger: {ledger_path}")
                previous_cost, positives = cost, found
                upcoming = next(iterator, None)
            yield {"method": summary["method"], "attributed_probe_cost": ceiling, "distinct_new_positive_c64": positives}
        if upcoming is not None or previous_cost != history + sent or positives != summary["distinct_new_positive_c64"]:
            raise ValueError(f"ledger and summary disagree: {ledger_path}")


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
    for method in METHODS:
        with (root / method / "summary.json").open(encoding="utf-8") as fh:
            summary = json.load(fh)
        if summary["method"] != method or summary["budget_total"] != config["budget_total_per_method"]:
            raise ValueError(f"inconsistent method or budget in {method} summary")
        expected_history = 149652 if method == "journal" else 0
        if summary["historical_attributed"] != expected_history or summary["attributed_total"] != expected_history + summary["formal_sent"]:
            raise ValueError(f"inconsistent cost accounting in {method} summary")
        summaries.append(summary)
    comparison_path = root / "comparison.csv"
    curve_path = root / "cost-discovery-curve.csv"
    if comparison_path.exists() or curve_path.exists():
        raise FileExistsError("refusing to overwrite formal comparison")
    curves = [row for summary in summaries for row in cost_curve(root / summary["method"] / "ledger.csv", summary, args.curve_interval)]
    with comparison_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("method", "budget_total", "historical_attributed", "formal_sent", "attributed_total", "budget_exhausted", "distinct_new_positive_c64", "probes_per_new_discovery", "stages"))
        writer.writeheader()
        for summary in summaries:
            discoveries = summary["distinct_new_positive_c64"]
            writer.writerow({**{key: summary[key] for key in ("method", "budget_total", "historical_attributed", "formal_sent", "attributed_total", "budget_exhausted", "distinct_new_positive_c64")}, "probes_per_new_discovery": summary["attributed_total"] / discoveries if discoveries else "", "stages": json.dumps(summary["stages"], sort_keys=True)})
    with curve_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("method", "attributed_probe_cost", "distinct_new_positive_c64"))
        writer.writeheader()
        writer.writerows(curves)
    print(comparison_path)
    print(curve_path)


if __name__ == "__main__":
    main()
