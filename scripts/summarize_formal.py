#!/usr/bin/env python3
"""Derive figure-ready aggregates from one completed formal method run.

Reads one method's committed `batch-*.tar.gz` archives and produces the two
aggregates that `analyze_formal.py` does not:

  - response composition    (response class and match status totals)
  - budget by prefix length (probes and positives grouped by prefixlen + mode)

The cumulative cost-discovery curve and the per-method comparison table are
already produced by `analyze_formal.py`; this script fills the remaining
figure/table inputs (Table III response composition, Fig. 4b budget placement).
"""

import argparse
import csv
import io
import ipaddress
import json
import tarfile
from pathlib import Path


def load_archives(method_dir):
    return sorted(Path(method_dir).glob("batch-*.tar.gz"))


def response_composition(archives):
    classes = {}
    statuses = {}
    observed_positive = 0
    multi = 0
    cross_class = 0
    for archive in archives:
        with tarfile.open(archive, "r:gz") as bundle:
            try:
                member = bundle.getmember("probes.csv.summary.json")
            except KeyError:
                continue
            with bundle.extractfile(member) as raw:
                summary = json.load(io.TextIOWrapper(raw, encoding="utf-8"))
        for cls, count in summary.get("response_class_counts", {}).items():
            classes[cls] = classes.get(cls, 0) + count
        for status, count in summary.get("match_status_counts", {}).items():
            statuses[status] = statuses.get(status, 0) + count
        observed_positive += summary.get("observed_positive_count", 0)
        multi += summary.get("multi_response_target_count", 0)
        cross_class += summary.get("cross_class_target_count", 0)
    return {
        "response_class_counts": classes,
        "match_status_counts": statuses,
        "observed_positive_count": observed_positive,
        "multi_response_target_count": multi,
        "cross_class_target_count": cross_class,
    }


def budget_by_prefixlen(archives):
    probes = {}       # prefixlen -> {mode: probes}
    positives = {}    # prefixlen -> positives
    for archive in archives:
        with tarfile.open(archive, "r:gz") as bundle:
            try:
                member = bundle.getmember("feedback.csv")
            except KeyError:
                continue
            with bundle.extractfile(member) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
                for row in reader:
                    length = ipaddress.ip_network(row["node"]).prefixlen
                    mode = row["mode"]
                    probes.setdefault(length, {}).setdefault(mode, 0)
                    probes[length][mode] += int(row["probes"])
                    positives[length] = positives.get(length, 0) + int(row["positives"])
    return {"probes": probes, "positives": positives}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("method_dir", help="method output directory containing batch-*.tar.gz")
    args = parser.parse_args()
    method_dir = Path(args.method_dir)
    if not method_dir.is_dir():
        parser.error(f"not a directory: {method_dir}")

    archives = load_archives(method_dir)
    if not archives:
        parser.error(f"no batch-*.tar.gz found under {method_dir}")

    result = {
        "method": method_dir.name,
        "response_composition": response_composition(archives),
        "budget_by_prefixlen": budget_by_prefixlen(archives),
    }

    out_path = method_dir / "figure_summary.json"
    if out_path.exists():
        parser.error(f"refusing to overwrite {out_path}")
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out_path)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
