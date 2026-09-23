#!/usr/bin/env python3
"""Run one frozen, independent formal search through the existing ZMap parser.

Each feedback round is one ZMap invocation (fixed target list plus one receive
tail), so the strategy feedback batch and the scanner file batch are the same
thing; the configurable `batch_size` sets that round.  Targets are generated
and written to disk as a stream, never materialised as full per-target lists.
Every successful archive contains the compact post-batch `state.json`; resume
loads that checkpoint without replaying historical probe rows.
"""

import argparse
import csv
import gzip
import importlib
import io
import ipaddress
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.common import Targets

STOP_REQUESTED = False
MANIFEST_FIELDS = ["probe_id", "node", "mode", "c64", "target_ipv6"]
ARCHIVE_FILES = (
    "manifest.csv",
    "raw-zmap.csv",
    "raw-zmap.csv.command.txt",
    "raw-zmap.csv.scanner-version.txt",
    "scan.log",
    "probes.csv",
    "probes.csv.summary.json",
    "feedback.csv",
    "last-hop-router-observations.csv",
    "state.json",
)


@dataclass
class BatchContext:
    parser: Path
    threshold: int
    strategy: object
    targets: Targets
    routers: set
    stages: Counter


def absolute(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def allowance_for(cfg):
    total = int(cfg["budget_total_per_method"])
    if not 0 < total <= 100_000_000_000:
        raise ValueError("formal method budget must be positive and stay within 100B")
    return total


def write_json_atomic(path, payload):
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def load_checkpoint(archives):
    """Load the latest committed checkpoint from its archive."""
    if not archives:
        raise ValueError("formal run has no committed batch to resume")
    with tarfile.open(archives[-1], "r:gz") as bundle:
        with bundle.extractfile("state.json") as fh:
            return json.load(io.TextIOWrapper(fh, encoding="utf-8"))


def parse_scanned_batch(folder, context):
    parsed = folder / "probes.csv"
    feedback = folder / "feedback.csv"
    router_obs = folder / "last-hop-router-observations.csv"
    for path in (parsed, Path(str(parsed) + ".summary.json"), feedback, router_obs):
        path.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, str(context.parser), str(folder / "manifest.csv"),
         str(folder / "raw-zmap.csv"), str(parsed), str(feedback), str(router_obs),
         "--slow-au-threshold-ms", str(context.threshold)],
        check=True, stdout=subprocess.DEVNULL, start_new_session=True,
    )


def apply_batch_feedback(folder, context):
    router_obs = folder / "last-hop-router-observations.csv"
    with open(router_obs, newline="", encoding="utf-8") as fh:
        context.routers.update(row["router_ipv6"] for row in csv.DictReader(fh))
    with open(folder / "feedback.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            context.strategy.feed_aggregate(
                row["node"], row["mode"], int(row["probes"]),
                int(row["positives"]), int(row["replies"]), json.loads(row["sources"]),
            )
    context.strategy.finish_batch()


def finish_scanned_batch(folder, archive, batch_number, context):
    parse_scanned_batch(folder, context)
    apply_batch_feedback(folder, context)
    checkpoint = {
        "strategy": context.strategy.snapshot(), "targets": context.targets.snapshot(),
        "last_hop_routers": sorted(context.routers),
        "batch_number": batch_number, "stages": dict(context.stages),
    }
    write_json_atomic(folder / "state.json", checkpoint)
    (folder / "sent-targets.txt").unlink(missing_ok=True)
    archive_batch(folder, archive)
    write_json_atomic(archive.parent / "state.json", checkpoint)


def replay_scanned_targets(folder, limit, context):
    """Advance compact state over a scanned batch without sending it again."""
    with open(folder / "manifest.csv", newline="", encoding="utf-8") as fh:
        recorded = csv.DictReader(fh)
        generated = context.strategy.iter_targets(limit)
        count = 0
        for row, item in itertools.zip_longest(recorded, generated):
            if row is None or item is None:
                raise ValueError("unfinished manifest does not match regenerated target count")
            c64, node, mode = item
            expected = (
                str(ipaddress.ip_network((c64 << 64, 64))), node, mode,
                context.targets.address(c64),
            )
            actual = (row["c64"], row["node"], row["mode"], row["target_ipv6"])
            if actual != expected:
                raise ValueError("unfinished manifest differs from compact pre-batch state")
            context.stages[mode] += 1
            count += 1
    if count == 0:
        raise ValueError("unfinished manifest is empty")


def run_method(config_path, method, resume=False):
    base = config_path.resolve().parent
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if method not in cfg["strategies"]:
        raise ValueError(f"method {method} is absent from formal configuration")
    allowance = allowance_for(cfg)
    if int(cfg["batch_size"]) <= 0:
        raise ValueError("batch_size must be positive")
    output = absolute(base, cfg["output_root"]) / method
    if resume:
        if not output.is_dir():
            raise FileNotFoundError(f"formal run does not exist: {output}")
        if json.loads((output / "formal.json").read_text(encoding="utf-8")) != cfg:
            raise ValueError("resume configuration differs from the recorded run")
        if (output / "summary.json").exists():
            raise FileExistsError(f"formal run is already complete: {output}")
    elif output.exists():
        raise FileExistsError(f"refusing to overwrite formal run: {output}")

    strategy_module = importlib.import_module(f"strategies.{method}")
    frame = strategy_module.load_frame(cfg, base)
    targets = Targets(f"{cfg['seed']}:{method}")
    strategy = strategy_module.Strategy(frame, targets, cfg["strategies"][method], allowance)

    scanner = cfg["scanner"]
    zmap = absolute(base, scanner["zmap_binary"])
    run_scan = Path(__file__).with_name("run_scan.sh")
    parsed_script = Path(__file__).with_name("parse_formal_results.py")

    last_hop_routers = set()
    stage_counts = Counter()
    batch_number = 0
    batch_context = BatchContext(
        parsed_script, cfg["slow_au_threshold_ms"], strategy, targets,
        last_hop_routers, stage_counts,
    )

    if resume:
        archives = sorted(output.glob("batch-*.tar.gz"))
        state = load_checkpoint(archives) if archives else None
        batch_number = state["batch_number"] if state else 0
        expected = [f"batch-{number:09d}.tar.gz" for number in range(1, batch_number + 1)]
        if [archive.name for archive in archives] != expected:
            raise ValueError("archive sequence does not match its latest checkpoint")
        parts = list(output.glob("batch-*.tar.gz.part"))
        if parts:
            raise ValueError(f"incomplete archive requires manual inspection: {parts[0]}")
        if state:
            strategy.restore(state["strategy"])
            targets.restore(state["targets"])
            last_hop_routers.update(state["last_hop_routers"])
            stage_counts.update(state["stages"])

        work = list(output.glob("batch-*.work"))
        if work:
            expected_work = output / f"batch-{batch_number + 1:09d}.work"
            if work != [expected_work]:
                raise ValueError("expected exactly the next unfinished batch work directory")
            required = ("manifest.csv", "raw-zmap.csv", "scan.log")
            if any(not (expected_work / name).is_file() for name in required):
                raise ValueError("unfinished batch has no complete scan evidence; inspect it manually")
            limit = min(int(cfg["batch_size"]), allowance - strategy.sent)
            replay_scanned_targets(expected_work, limit, batch_context)
            batch_number += 1
            finish_scanned_batch(
                expected_work, output / f"batch-{batch_number:09d}.tar.gz",
                batch_number, batch_context,
            )
    else:
        output.mkdir(parents=True)
        shutil.copyfile(config_path, output / "formal.json")

    while strategy.sent < allowance:
        if STOP_REQUESTED:
            break
        limit = min(int(cfg["batch_size"]), allowance - strategy.sent)
        batch_number += 1
        round_name = f"batch-{batch_number:09d}"
        folder = output / f"{round_name}.work"
        archive = output / f"{round_name}.tar.gz"
        folder.mkdir()

        manifest = folder / "manifest.csv"
        sent = folder / "sent-targets.txt"
        raw = folder / "raw-zmap.csv"

        # Stream target generation; no in-memory batch lists.
        count = 0
        with open(manifest, "w", newline="", encoding="utf-8") as mf, open(sent, "w", encoding="utf-8") as sf:
            writer = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for index, (c64, node, mode) in enumerate(strategy.iter_targets(limit)):
                address = targets.address(c64)
                writer.writerow({
                    "probe_id": f"{method}:{round_name}:{index}",
                    "node": node, "mode": mode,
                    "c64": str(ipaddress.ip_network((c64 << 64, 64))),
                    "target_ipv6": address,
                })
                sf.write(address + "\n")
                stage_counts[mode] += 1
                count += 1

        if count == 0:
            # Candidates exhausted; nothing was sent, so drop the empty batch.
            shutil.rmtree(folder)
            batch_number -= 1
            break

        command = [
            "bash", str(run_scan), str(zmap), str(sent), str(raw),
            scanner["source_ipv6"], scanner["interface"], scanner["gateway_mac"],
            str(scanner["rate_pps"]), str(scanner["cooldown_seconds"]),
        ]
        with open(folder / "scan.log", "w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finish_scanned_batch(
            folder, archive, batch_number, batch_context,
        )

    if STOP_REQUESTED:
        return {"status": "paused", "method": method, "completed_batches": batch_number, "formal_sent": strategy.sent}

    native = getattr(strategy, "native_prefixes", [])
    if native:
        with gzip.open(output / "native-prefixes.txt.gz", "wt", encoding="utf-8") as fh:
            fh.writelines(str(prefix) + "\n" for prefix in native)
    with gzip.open(output / "last-hop-routers.txt.gz", "wt", encoding="utf-8") as fh:
        fh.writelines(router + "\n" for router in sorted(last_hop_routers, key=ipaddress.IPv6Address))
    summary = {
        "method": method,
        "started_from": "RIS BGP only",
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "budget_total": allowance,
        "formal_sent": strategy.sent,
        "budget_exhausted": strategy.sent == allowance,
        "distinct_new_positive_c64": strategy.positive_count,
        "distinct_last_hop_router_addresses": len(last_hop_routers),
        "stages": dict(stage_counts),
        "native_prefix_count": len(native),
    }
    write_json_atomic(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("method", help="strategy module name under strategies/")
    parser.add_argument(
        "--resume", action="store_true",
        help="continue from the newest archived compact checkpoint",
    )
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
