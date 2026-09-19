#!/usr/bin/env python3
"""Run one named scan round from the single campaign configuration.

Applies the scan denylist at whole-panel granularity (drop a panel if any of
its targets is covered), writes the effective manifest and excluded-panel
table, preserves the planned round's global shuffled order, and delegates the
actual scan to run_scan.sh. Scanning, parsing, and analysis live elsewhere.
"""

import argparse
import csv
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict


def load_exclusions(path):
    """IPv6 networks from a denylist file: one prefix per line, '#' comments."""
    exclusions = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            exclusions.append(ipaddress.ip_network(text, strict=False))
    return exclusions


def load_targets(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_json")
    parser.add_argument("round")
    args = parser.parse_args(argv)

    with open(args.campaign_json, encoding="utf-8") as fh:
        cfg = json.load(fh)

    manifest_path = cfg["input"]["target_manifest"]
    targets = load_targets(manifest_path)
    exclusions = load_exclusions(cfg["safety"]["scan_exclusions"])

    by_panel = defaultdict(list)
    for row in targets:
        by_panel[row["panel_id"]].append(row)

    excluded_panel_ids = {
        panel_id
        for panel_id, rows in by_panel.items()
        if any(
            ipaddress.ip_address(row["target_ipv6"]) in prefix
            for row in rows
            for prefix in exclusions
        )
    }

    effective_rows = [
        row for row in targets if row["panel_id"] not in excluded_panel_ids
    ]
    excluded_rows = [by_panel[panel_id][0] for panel_id in sorted(excluded_panel_ids)]

    output_root = cfg["output_root"]
    os.makedirs(output_root, exist_ok=True)
    fieldnames = list(targets[0].keys())

    effective_path = os.path.join(output_root, "effective_calibration_targets.csv")
    with open(effective_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(effective_rows)

    excluded_path = os.path.join(output_root, "excluded_panels.csv")
    with open(excluded_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(excluded_rows)

    # Preserve the planned round's global hash-shuffled order by filtering the
    # already-generated target file rather than re-deriving the shuffle here.
    planned_file = os.path.join(
        os.path.dirname(manifest_path), f"targets-{args.round}.txt"
    )
    planned = read_lines(planned_file)
    target_to_panel = {row["target_ipv6"]: row["panel_id"] for row in targets}
    sent = [
        address
        for address in planned
        if target_to_panel.get(address) not in excluded_panel_ids
    ]

    round_dir = os.path.join(output_root, args.round)
    os.makedirs(round_dir, exist_ok=True)
    sent_path = os.path.join(round_dir, f"sent-targets-{args.round}.txt")
    with open(sent_path, "w", encoding="utf-8") as fh:
        for address in sent:
            fh.write(f"{address}\n")

    shutil.copyfile(args.campaign_json, os.path.join(round_dir, "campaign.json"))

    raw_output = os.path.join(round_dir, f"raw-{args.round}.csv")
    run_scan = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_scan.sh")
    subprocess.run(
        [
            "bash",
            run_scan,
            cfg["scanner"]["zmap_binary"],
            sent_path,
            raw_output,
            cfg["scanner"]["source_ipv6"],
            cfg["scanner"]["interface"],
            cfg["scanner"]["gateway_mac"],
            str(cfg["scanner"]["rate_pps"]),
            str(cfg["scanner"]["cooldown_seconds"]),
        ],
        check=True,
    )

    print(
        f"round={args.round} sent={len(sent)} "
        f"excluded_panels={len(excluded_panel_ids)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
