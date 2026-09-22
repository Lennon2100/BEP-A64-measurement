#!/usr/bin/env python3
"""Run one frozen, independent formal search through the existing ZMap parser."""

import argparse
import csv
import importlib
import ipaddress
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.common import Frame, Targets, read_prefixes, read_prior_c64s
from parse_results import PLAN_FIELDS


def absolute(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def validate(cfg, base, method):
    if method not in ("journal", "tnet", "subrecon"):
        raise ValueError(f"unknown method: {method}")
    if not cfg["input"]["ris_snapshots"]:
        raise ValueError("record exact RIS collector RIB identities before scanning")
    for key in ("bgp_tree_csv", "prior_c64_manifest", "scan_exclusions"):
        if not absolute(base, cfg["input"][key]).is_file():
            raise ValueError(f"missing input {key}: {cfg['input'][key]}")
    scanner = cfg["scanner"]
    if int(scanner["rate_pps"]) <= 0 or int(scanner["cooldown_seconds"]) < 0:
        raise ValueError("formal scanner needs a finite positive packet rate")
    if int(scanner["probes"]) != 1:
        raise ValueError("formal comparison requires one probe per target")
    if ipaddress.ip_address(scanner["source_ipv6"]).version != 6:
        raise ValueError("source address must be IPv6")
    if not scanner["interface"] or not scanner["gateway_mac"]:
        raise ValueError("scanner interface and gateway mode are required")
    total = int(cfg["budget_total_per_method"])
    historical = int(cfg["journal_historical_probes"])
    if historical != 149652 or total <= historical:
        raise ValueError("budget must include the fixed 149652 D050 journal probes")
    allowance = total - historical if method == "journal" else total
    if method == "tnet" and not 0 < int(cfg["strategies"]["tnet"]["screen_budget"]) < allowance:
        raise ValueError("TNet needs a charged screen budget below its total")
    if int(cfg["batch_size"]) <= 0:
        raise ValueError("batch_size must be positive")
    return allowance


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_method(config_path, method):
    base = config_path.resolve().parent
    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    allowance = validate(cfg, base, method)
    output = absolute(base, cfg["output_root"]) / method
    if output.exists():
        raise FileExistsError(f"refusing to overwrite formal run: {output}")

    frame = Frame(absolute(base, cfg["input"]["bgp_tree_csv"]))
    exclusions = read_prefixes(absolute(base, cfg["input"]["scan_exclusions"]))
    prior = read_prior_c64s(absolute(base, cfg["input"]["prior_c64_manifest"]))
    targets = Targets(f"{cfg['seed']}:{method}", exclusions, prior)
    strategy_class = importlib.import_module(f"strategies.{method}").Strategy
    strategy = strategy_class(frame, targets, cfg["strategies"][method], allowance)
    output.mkdir(parents=True)
    shutil.copyfile(config_path, output / "formal.json")
    scanner = cfg["scanner"]
    zmap = absolute(base, scanner["zmap_binary"])
    run_scan = Path(__file__).with_name("run_scan.sh")
    parsed_script = Path(__file__).with_name("parse_results.py")
    observed = set()
    ledger_path = output / "ledger.csv"
    ledger_fields = ("probe_number", "attributed_total", "stage", "probe_id", "c64", "response_class", "positive", "distinct_positive", "batch")
    write_csv(ledger_path, ledger_fields, [])
    stage_counts = Counter()
    batch_number = 0
    sent_count = 0
    while sent_count < allowance:
        batch = strategy.next_batch(min(int(cfg["batch_size"]), allowance - sent_count))
        if not batch:
            break
        batch_number += 1
        round_name = f"batch-{batch_number:06d}"
        folder = output / round_name
        folder.mkdir()
        plan = []
        metadata = []
        for index, item in enumerate(batch):
            c64 = str(ipaddress.ip_network((item["c64"] << 64, 64)))
            address = targets.address(item["c64"])
            row = dict.fromkeys(PLAN_FIELDS, "")
            row.update(probe_id=f"{method}:{round_name}:{index}", panel_id=f"{method}:{round_name}:{index}", c64=c64, target_ipv6=address, role="search", round=round_name, selection_arm=method)
            plan.append(row)
            metadata.append({"probe_id": row["probe_id"], "stage": item["stage"], "node": item["node"], "c64": c64, "target_ipv6": address})
        manifest = folder / "targets.csv"
        sent = folder / "sent-targets.txt"
        raw = folder / "raw-zmap.csv"
        parsed = folder / "probes.csv"
        write_csv(manifest, PLAN_FIELDS, plan)
        write_csv(folder / "target-metadata.csv", list(metadata[0]), metadata)
        sent.write_text("".join(row["target_ipv6"] + "\n" for row in plan), encoding="utf-8")
        command = ["bash", str(run_scan), str(zmap), str(sent), str(raw), scanner["source_ipv6"], scanner["interface"], scanner["gateway_mac"], str(scanner["rate_pps"]), str(scanner["cooldown_seconds"])]
        with open(folder / "scan.log", "w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        subprocess.run([sys.executable, str(parsed_script), str(manifest), round_name, str(raw), str(parsed), "--slow-au-threshold-ms", str(cfg["slow_au_threshold_ms"])], check=True, stdout=subprocess.DEVNULL)
        with open(parsed, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if len(rows) != len(batch) or any(row["match_status"] == "unmatched" for row in rows):
            raise ValueError(f"parser mismatch in {round_name}; inspect immutable raw output")
        strategy.feedback(rows)
        ledger_batch = []
        for item, meta, row in zip(batch, metadata, rows):
            sent_count += 1
            positive = row["is_observed_positive"] == "1"
            if positive:
                observed.add(item["c64"])
            stage_counts[item["stage"]] += 1
            ledger_batch.append({"probe_number": sent_count, "attributed_total": sent_count + (149652 if method == "journal" else 0), "stage": item["stage"], "probe_id": meta["probe_id"], "c64": meta["c64"], "response_class": row["response_class"], "positive": int(positive), "distinct_positive": len(observed), "batch": round_name})
        with open(ledger_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=ledger_fields)
            writer.writerows(ledger_batch)
    native = getattr(strategy, "native_prefixes", getattr(strategy, "regions", []))
    if native:
        (output / "native-prefixes.txt").write_text("".join(str(prefix) + "\n" for prefix in native), encoding="utf-8")
    summary = {"method": method, "started_from": "RIS BGP only", "finished_utc": datetime.now(timezone.utc).isoformat(), "budget_total": int(cfg["budget_total_per_method"]), "historical_attributed": 149652 if method == "journal" else 0, "formal_sent": sent_count, "attributed_total": sent_count + (149652 if method == "journal" else 0), "budget_exhausted": sent_count == allowance, "distinct_new_positive_c64": len(observed), "stages": dict(stage_counts), "native_prefix_count": len(native)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("method", choices=("journal", "tnet", "subrecon"))
    args = parser.parse_args()
    try:
        print(json.dumps(run_method(Path(args.config), args.method), indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
