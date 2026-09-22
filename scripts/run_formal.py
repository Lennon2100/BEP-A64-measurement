#!/usr/bin/env python3
"""Run one frozen, independent formal search through the existing ZMap parser."""

import argparse
import csv
import gzip
import importlib
import io
import ipaddress
import json
import os
import random
import signal
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.common import Targets, read_prior_c64s
from parse_results import PLAN_FIELDS


STOP_REQUESTED = False
LEDGER_FIELDS = ("probe_number", "attributed_total", "stage", "probe_id", "c64", "response_class", "positive", "distinct_positive", "batch")
ARCHIVE_FILES = (
    "targets.csv", "target-metadata.csv", "sent-targets.txt", "raw-zmap.csv",
    "raw-zmap.csv.command.txt", "raw-zmap.csv.scanner-version.txt", "scan.log",
    "probes.csv", "probes.csv.summary.json", "last-hop-router-observations.csv",
    "ledger.csv",
)


def absolute(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def allowance_for(cfg, method):
    total = int(cfg["budget_total_per_method"])
    historical = int(cfg["historical_probes"].get(method, 0))
    if not 0 < total <= 100_000_000_000 or total <= historical:
        raise ValueError("formal method budget must exceed historical cost and stay within 100B")
    return total - historical


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_batch_archive(path):
    wanted = {"targets.csv", "target-metadata.csv", "probes.csv", "last-hop-router-observations.csv"}
    tables = {}
    with tarfile.open(path, "r:gz") as bundle:
        if set(bundle.getnames()) != set(ARCHIVE_FILES):
            raise ValueError(f"incomplete batch archive: {path}")
        for member in bundle:
            if member.name in wanted:
                with bundle.extractfile(member) as fh:
                    tables[member.name] = list(csv.DictReader(io.StringIO(fh.read().decode("utf-8"))))
    if set(tables) != wanted:
        raise ValueError(f"incomplete batch archive: {path}")
    return tables


def archive_batch(folder, archive):
    missing = [name for name in ARCHIVE_FILES if not (folder / name).is_file()]
    if missing:
        raise ValueError(f"batch is incomplete, refusing to archive: {missing}")
    temporary = archive.with_name(archive.name + ".part")
    if temporary.exists() or archive.exists():
        raise FileExistsError(f"batch archive already exists: {archive}")
    with tarfile.open(temporary, "w:gz") as bundle:
        for name in ARCHIVE_FILES:
            bundle.add(folder / name, arcname=name)
    with tarfile.open(temporary, "r:gz") as bundle:
        if set(bundle.getnames()) != set(ARCHIVE_FILES):
            raise ValueError(f"batch archive is incomplete: {temporary}")
        for member in bundle:
            with bundle.extractfile(member) as fh:
                while fh.read(1024 * 1024):
                    pass
    os.replace(temporary, archive)
    if folder.resolve().parent != archive.parent.resolve():
        raise ValueError(f"batch work directory is outside its run: {folder}")
    shutil.rmtree(folder)


def account_batch(batch, metadata, rows, sent_count, historical, observed, stages, round_name):
    ledger_rows = []
    for target, meta, row in zip(batch, metadata, rows):
        sent_count += 1
        positive = row["is_observed_positive"] == "1"
        if positive:
            observed.add(target["c64"])
        stages[target["stage"]] += 1
        ledger_rows.append({"probe_number": sent_count, "attributed_total": sent_count + historical,
                            "stage": target["stage"], "probe_id": meta["probe_id"],
                            "c64": meta["c64"], "response_class": row["response_class"],
                            "positive": int(positive), "distinct_positive": len(observed), "batch": round_name})
    return sent_count, ledger_rows


def run_method(config_path, method, resume=False):
    base = config_path.resolve().parent
    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if method not in cfg["strategies"]:
        raise ValueError(f"method {method} is absent from formal configuration")
    allowance = allowance_for(cfg, method)
    historical = int(cfg["historical_probes"].get(method, 0))
    output = absolute(base, cfg["output_root"]) / method
    if resume:
        if not output.is_dir():
            raise FileNotFoundError(f"formal run does not exist: {output}")
        with (output / "formal.json").open(encoding="utf-8") as fh:
            if json.load(fh) != cfg:
                raise ValueError("resume configuration differs from the recorded run")
        if (output / "summary.json").exists():
            raise FileExistsError(f"formal run is already complete: {output}")
    elif output.exists():
        raise FileExistsError(f"refusing to overwrite formal run: {output}")
    if int(cfg["batch_size"]) <= 0:
        raise ValueError("batch_size must be positive")

    strategy_module = importlib.import_module(f"strategies.{method}")
    frame = strategy_module.load_frame(cfg, base)
    prior = read_prior_c64s(absolute(base, cfg["input"]["prior_c64_manifest"]))
    targets = Targets(f"{cfg['seed']}:{method}", prior)
    strategy = strategy_module.Strategy(frame, targets, cfg["strategies"][method], allowance)
    if not resume:
        output.mkdir(parents=True)
        shutil.copyfile(config_path, output / "formal.json")
    scanner = cfg["scanner"]
    zmap = absolute(base, scanner["zmap_binary"])
    run_scan = Path(__file__).with_name("run_scan.sh")
    parsed_script = Path(__file__).with_name("parse_results.py")
    observed = set()
    last_hop_routers = set()
    stage_counts = Counter()
    batch_number = 0
    sent_count = 0
    if resume:
        archives = sorted(output.glob("batch-*.tar.gz"))
        for number, archive in enumerate(archives, 1):
            round_name = f"batch-{number:09d}"
            if archive.name != f"{round_name}.tar.gz":
                raise ValueError(f"noncontiguous batch archives in {output}")
            tables = read_batch_archive(archive)
            metadata = tables["target-metadata.csv"]
            planned = tables["targets.csv"]
            batch = strategy.next_batch(min(int(cfg["batch_size"]), allowance - sent_count))
            if len(batch) != len(metadata) or len(planned) != len(batch):
                raise ValueError(f"batch length differs during resume: {archive}")
            for index, (target, meta, plan) in enumerate(zip(batch, metadata, planned)):
                address = targets.address(target["c64"])
                expected_c64 = str(ipaddress.ip_network((target["c64"] << 64, 64)))
                expected_id = f"{method}:{round_name}:{index}"
                if (meta["c64"], meta["node"], meta["stage"], meta["target_ipv6"], meta["probe_id"]) != (expected_c64, target["node"], target["stage"], address, expected_id):
                    raise ValueError(f"strategy state differs from recorded batch: {archive}")
                if (plan["c64"], plan["target_ipv6"], plan["probe_id"]) != (expected_c64, address, expected_id):
                    raise ValueError(f"target manifest differs from recorded batch: {archive}")
            parsed_by_probe = {row["probe_id"]: row for row in tables["probes.csv"] if row["probe_id"]}
            rows = [parsed_by_probe[meta["probe_id"]] for meta in metadata]
            strategy.feedback(rows)
            sent_count, _ = account_batch(batch, metadata, rows, sent_count, historical, observed, stage_counts, round_name)
            last_hop_routers.update(row["router_ipv6"] for row in tables["last-hop-router-observations.csv"])
            batch_number = number
            work_folder = output / f"{round_name}.work"
            if work_folder.is_dir():
                if work_folder.resolve().parent != output.resolve():
                    raise ValueError(f"batch work directory is outside its run: {work_folder}")
                shutil.rmtree(work_folder)
        unfinished = list(output.glob("batch-*.work")) + list(output.glob("batch-*.tar.gz.part"))
        if unfinished:
            raise ValueError(f"unfinished batch cannot be replayed or reprobed automatically: {unfinished[0]}")
    while sent_count < allowance:
        if STOP_REQUESTED:
            break
        batch = strategy.next_batch(min(int(cfg["batch_size"]), allowance - sent_count))
        if not batch:
            break
        batch_number += 1
        round_name = f"batch-{batch_number:09d}"
        folder = output / f"{round_name}.work"
        archive = output / f"{round_name}.tar.gz"
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
        router_observations = folder / "last-hop-router-observations.csv"
        write_csv(manifest, PLAN_FIELDS, plan)
        write_csv(folder / "target-metadata.csv", list(metadata[0]), metadata)
        sent_addresses = [row["target_ipv6"] for row in plan]
        random.Random(f"{cfg['seed']}:{method}:{round_name}").shuffle(sent_addresses)
        sent.write_text("".join(address + "\n" for address in sent_addresses), encoding="utf-8")
        command = ["bash", str(run_scan), str(zmap), str(sent), str(raw), scanner["source_ipv6"], scanner["interface"], scanner["gateway_mac"], str(scanner["rate_pps"]), str(scanner["cooldown_seconds"])]
        with open(folder / "scan.log", "w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        subprocess.run([sys.executable, str(parsed_script), str(manifest), round_name, str(raw), str(parsed), "--slow-au-threshold-ms", str(cfg["slow_au_threshold_ms"]), "--last-hop-router-output", str(router_observations)], check=True, stdout=subprocess.DEVNULL, start_new_session=True)
        with open(router_observations, newline="", encoding="utf-8") as fh:
            last_hop_routers.update(row["router_ipv6"] for row in csv.DictReader(fh))
        with open(parsed, newline="", encoding="utf-8") as fh:
            parsed_by_probe = {row["probe_id"]: row for row in csv.DictReader(fh) if row["probe_id"]}
        rows = [parsed_by_probe[entry["probe_id"]] for entry in metadata]
        strategy.feedback(rows)
        sent_count, ledger_batch = account_batch(batch, metadata, rows, sent_count, historical, observed, stage_counts, round_name)
        write_csv(folder / "ledger.csv", LEDGER_FIELDS, ledger_batch)
        archive_batch(folder, archive)
    if STOP_REQUESTED:
        return {"status": "paused", "method": method, "completed_batches": batch_number, "formal_sent": sent_count, "attributed_total": sent_count + historical}
    native = getattr(strategy, "native_prefixes", getattr(strategy, "regions", []))
    if native:
        with gzip.open(output / "native-prefixes.txt.gz", "wt", encoding="utf-8") as fh:
            fh.writelines(str(prefix) + "\n" for prefix in native)
    with gzip.open(output / "last-hop-routers.txt.gz", "wt", encoding="utf-8") as fh:
        fh.writelines(router + "\n" for router in sorted(last_hop_routers, key=ipaddress.IPv6Address))
    summary = {"method": method, "started_from": "RIS BGP only", "finished_utc": datetime.now(timezone.utc).isoformat(), "budget_total": int(cfg["budget_total_per_method"]), "historical_attributed": historical, "formal_sent": sent_count, "attributed_total": sent_count + historical, "budget_exhausted": sent_count == allowance, "distinct_new_positive_c64": len(observed), "distinct_last_hop_router_addresses": len(last_hop_routers), "stages": dict(stage_counts), "native_prefix_count": len(native)}
    summary_part = output / "summary.json.part"
    summary_part.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    os.replace(summary_part, output / "summary.json")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("method", help="strategy module name under strategies/")
    parser.add_argument("--resume", action="store_true", help="continue after the last compressed batch")
    args = parser.parse_args()
    def request_stop(signum, frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        print(json.dumps(run_method(Path(args.config), args.method, args.resume), indent=2))
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
