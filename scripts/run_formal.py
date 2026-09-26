#!/usr/bin/env python3
"""Run one independent measurement strategy through the ZMap parser.

Each feedback round is one ZMap invocation (fixed target list plus one receive
tail), so the strategy feedback batch and the scanner file batch are the same
thing. `batch_size` sets its maximum size, and a strategy may impose a smaller
round ceiling. Targets are generated and written to disk as a stream, never
materialised as full per-target lists.
Every successful archive contains the compact post-batch `state.pkl.gz`; resume
loads that checkpoint without replaying historical probe rows.
"""

import argparse
import gzip
import importlib
import io
import ipaddress
import json
import os
import pickle
import shutil
import signal
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import resource
except ImportError:  # Windows development host; formal runs use Linux.
    resource = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.common import Targets
from parquet_io import (
    StringParquetWriter,
    iter_archive_table,
    iter_parquet_rows,
    iter_table_file,
)

STOP_REQUESTED = False
MANIFEST_FIELDS = ["probe_id", "node", "mode", "c64", "target_ipv6"]
ARCHIVE_FILES = (
    "manifest.parquet",
    "raw-zmap.csv",
    "raw-zmap.csv.command.txt",
    "raw-zmap.csv.scanner-version.txt",
    "scan.log",
    "probes.parquet",
    "probes.parquet.summary.json",
    "feedback.parquet",
    "last-hop-router-observations.parquet",
)
CHECKPOINT_NAME = "state.pkl.gz"


@dataclass
class BatchContext:
    parser: Path
    threshold: int
    strategy: object
    targets: Targets
    routers: set
    stages: Counter
    legacy_recovery: bool = False
    started_utc: str | None = None


def absolute(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def allowance_for(cfg):
    total = int(cfg["budget_total_per_method"])
    if not 0 < total <= 100_000_000_000:
        raise ValueError("method budget must be positive and stay within 100B")
    return total


def write_json_atomic(path, payload):
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def write_checkpoint_atomic(path, payload):
    tmp = path.with_name(path.name + ".part")
    with gzip.open(tmp, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def read_checkpoint_file(path):
    with gzip.open(path, "rb") as fh:
        return pickle.load(fh)


def checkpoint_payload(context, batch_number):
    return {
        "strategy": context.strategy.checkpoint(),
        "targets": context.targets.checkpoint(),
        "batch_number": batch_number,
        "stages": dict(context.stages),
        "started_utc": context.started_utc,
    }


def log_memory(stage):
    if resource is not None:
        peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"memory {stage}: peak_rss={peak_mib:.1f} MiB", file=sys.stderr, flush=True)


def archive_batch(folder, archive):
    archive_files = ARCHIVE_FILES + (CHECKPOINT_NAME,)
    missing = [name for name in archive_files if not (folder / name).is_file()]
    if missing:
        raise ValueError(f"batch is incomplete, refusing to archive: {missing}")
    temporary = archive.with_name(archive.name + ".part")
    if temporary.exists() or archive.exists():
        raise FileExistsError(f"batch archive already exists: {archive}")
    with tarfile.open(temporary, "w:gz") as bundle:
        for name in archive_files:
            bundle.add(folder / name, arcname=name)
    with tarfile.open(temporary, "r:gz") as bundle:
        if set(bundle.getnames()) != set(archive_files):
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
        raise ValueError("measurement run has no committed batch to resume")
    with tarfile.open(archives[-1], "r:gz") as bundle:
        names = set(bundle.getnames())
        if CHECKPOINT_NAME in names:
            with bundle.extractfile(CHECKPOINT_NAME) as raw:
                with gzip.GzipFile(fileobj=raw) as fh:
                    return pickle.load(fh)
        with bundle.extractfile("state.json") as fh:
            return json.load(io.TextIOWrapper(fh, encoding="utf-8"))


def load_recovered_targets(output, targets):
    recovery_batches = list(targets.recovery_batches)
    for batch_number in recovery_batches:
        archive = output / f"batch-{batch_number:09d}.tar.gz"
        with tarfile.open(archive, "r:gz") as bundle:
            c64_values = [
                int(ipaddress.ip_network(row["c64"]).network_address) >> 64
                for row in iter_archive_table(bundle, "manifest.parquet", "manifest.csv")
            ]
        c64_values.sort()
        targets.add_recovered(c64_values, batch_number, reseed=False)


def parse_scanned_batch(folder, context):
    parsed = folder / "probes.parquet"
    feedback = folder / "feedback.parquet"
    router_obs = folder / "last-hop-router-observations.parquet"
    for path in (parsed, Path(str(parsed) + ".summary.json"), feedback, router_obs):
        path.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, str(context.parser), str(folder / "manifest.parquet"),
         str(folder / "raw-zmap.csv"), str(parsed), str(feedback), str(router_obs),
         "--slow-au-threshold-ms", str(context.threshold)],
        check=True, stdout=subprocess.DEVNULL, start_new_session=True,
    )


def apply_batch_feedback(folder, context):
    router_obs = folder / "last-hop-router-observations.parquet"
    feed_router = getattr(context.strategy, "feed_router_observation", None)
    for row in iter_parquet_rows(router_obs):
        router = int(ipaddress.IPv6Address(row["router_ipv6"]))
        context.routers.add(router)
        if feed_router is not None:
            feed_router(row["c64"], router)
    feed_class = getattr(context.strategy, "feed_class_aggregate", None)
    for row in iter_parquet_rows(folder / "feedback.parquet"):
        if feed_class is not None:
            feed_class(row)
        context.strategy.feed_aggregate(
            row["node"], row["mode"], int(row["probes"]),
            int(row["positives"]), int(row["replies"]), json.loads(row["sources"]),
        )
    context.strategy.finish_batch()


def finish_scanned_batch(folder, archive, batch_number, context):
    parse_scanned_batch(folder, context)
    apply_batch_feedback(folder, context)
    if context.legacy_recovery:
        rebuild = getattr(context.strategy, "rebuild_after_recovery", None)
        if rebuild is not None:
            rebuild()
        context.legacy_recovery = False
    checkpoint = checkpoint_payload(context, batch_number)
    checkpoint["last_hop_routers"] = context.routers
    write_checkpoint_atomic(folder / CHECKPOINT_NAME, checkpoint)
    (folder / "sent-targets.txt").unlink(missing_ok=True)
    archive_batch(folder, archive)
    write_checkpoint_atomic(archive.parent / CHECKPOINT_NAME, checkpoint)
    log_memory(f"after batch {batch_number}")


def adopt_scanned_manifest(folder, batch_number, context):
    """Adopt one legacy scanned batch whose prepared checkpoint is absent."""
    c64_values = []
    actions = []
    seen_actions = set()
    for row in iter_table_file(folder / "manifest.parquet", folder / "manifest.csv"):
        c64_values.append(int(ipaddress.ip_network(row["c64"]).network_address) >> 64)
        action = (row["node"], row["mode"])
        if action not in seen_actions:
            actions.append(action)
            seen_actions.add(action)
        context.stages[row["mode"]] += 1
    if not c64_values:
        raise ValueError("unfinished manifest is empty")
    c64_values.sort()
    context.strategy.adopt_manifest_actions(actions)
    context.targets.add_recovered(c64_values, batch_number)
    context.legacy_recovery = True


def run_method(config_path, method, resume=False):
    base = config_path.resolve().parent
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if method not in cfg["strategies"]:
        raise ValueError(f"method {method} is absent from the configuration")
    allowance = allowance_for(cfg)
    if int(cfg["batch_size"]) <= 0:
        raise ValueError("batch_size must be positive")
    output = absolute(base, cfg["output_root"]) / method
    if resume:
        if not output.is_dir():
            raise FileNotFoundError(f"measurement run does not exist: {output}")
        if json.loads((output / "formal.json").read_text(encoding="utf-8")) != cfg:
            raise ValueError("resume configuration differs from the recorded run")
        if (output / "summary.json").exists():
            raise FileExistsError(f"measurement run is already complete: {output}")
    elif output.exists():
        raise FileExistsError(f"refusing to overwrite measurement run: {output}")

    strategy_module = importlib.import_module(f"strategies.{method}")
    frame = strategy_module.load_frame(cfg, base)
    targets = Targets(f"{cfg['seed']}:{method}")
    strategy = strategy_module.Strategy(frame, targets, cfg["strategies"][method], allowance)
    minimum_batch_size = getattr(strategy, "minimum_batch_size", 1)
    if int(cfg["batch_size"]) < minimum_batch_size:
        raise ValueError(
            f"{method} requires batch_size >= {minimum_batch_size} "
            "to keep one strategy action intact"
        )

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
            compact = getattr(strategy, "compact_restored_state", None)
            if compact is not None:
                compact()
            load_recovered_targets(output, targets)
            last_hop_routers.update(
                int(ipaddress.IPv6Address(router))
                for router in state["last_hop_routers"]
            )
            stage_counts.update(state["stages"])
            batch_context.started_utc = state.get("started_utc")
            del state
            log_memory("after restoring committed checkpoint")

        work = sorted(output.glob("batch-*.work"))
        if work:
            expected_work = output / f"batch-{batch_number + 1:09d}.work"
            if work != [expected_work]:
                raise ValueError("expected exactly the next unfinished batch work directory")
            scan_artifacts = ("raw-zmap.csv", "scan.log", "scan-complete")
            has_manifest = (
                (expected_work / "manifest.parquet").is_file()
                or (expected_work / "manifest.csv").is_file()
            )
            if (
                has_manifest
                and (expected_work / "sent-targets.txt").is_file()
                and not any((expected_work / name).exists() for name in scan_artifacts)
            ):
                print(
                    f"discarding unscanned {expected_work.name} after interrupted checkpoint",
                    file=sys.stderr, flush=True,
                )
                shutil.rmtree(expected_work)
                work = []
        if work:
            expected_work = work[0]
            required = ("raw-zmap.csv", "scan.log")
            if any(not (expected_work / name).is_file() for name in required):
                raise ValueError("unfinished batch has no complete scan evidence; inspect it manually")
            has_manifest = (
                (expected_work / "manifest.parquet").is_file()
                or (expected_work / "manifest.csv").is_file()
            )
            if not has_manifest:
                raise ValueError("unfinished batch has no manifest; inspect it manually")
            scan_completed = (expected_work / "scan-complete").is_file()
            legacy_parser_started = (
                (expected_work / "probes.parquet").is_file()
                or (expected_work / "probes.csv").is_file()
            )
            if not scan_completed and not legacy_parser_started:
                raise ValueError("unfinished batch has no completed-scan marker; inspect it manually")
            prepared_path = expected_work / "prepared-state.pkl.gz"
            legacy_prepared_path = expected_work / "prepared-state.json"
            if prepared_path.is_file() or legacy_prepared_path.is_file():
                prepared = (
                    read_checkpoint_file(prepared_path)
                    if prepared_path.is_file()
                    else json.loads(legacy_prepared_path.read_text(encoding="utf-8"))
                )
                strategy.restore(prepared["strategy"])
                targets.restore(prepared["targets"])
                load_recovered_targets(output, targets)
                stage_counts.clear()
                stage_counts.update(prepared["stages"])
                batch_context.started_utc = prepared.get("started_utc", batch_context.started_utc)
                del prepared
            else:
                print(f"adopting already-sent {expected_work.name}", file=sys.stderr, flush=True)
                adopt_scanned_manifest(expected_work, batch_number + 1, batch_context)
            batch_number += 1
            print(f"parsing existing raw output for {expected_work.name}", file=sys.stderr, flush=True)
            finish_scanned_batch(
                expected_work, output / f"batch-{batch_number:09d}.tar.gz",
                batch_number, batch_context,
            )
    else:
        output.mkdir(parents=True)
        shutil.copyfile(config_path, output / "formal.json")
        batch_context.started_utc = datetime.now(timezone.utc).isoformat()

    while strategy.sent < allowance:
        if STOP_REQUESTED:
            break
        limit = min(int(cfg["batch_size"]), allowance - strategy.sent)
        batch_number += 1
        round_name = f"batch-{batch_number:09d}"
        folder = output / f"{round_name}.work"
        archive = output / f"{round_name}.tar.gz"
        folder.mkdir()

        manifest = folder / "manifest.parquet"
        sent = folder / "sent-targets.txt"
        raw = folder / "raw-zmap.csv"

        # Stream target generation; no in-memory batch lists.
        count = 0
        manifest_writer = StringParquetWriter(manifest, MANIFEST_FIELDS)
        with open(sent, "w", encoding="utf-8") as sf:
            for index, (c64, node, mode) in enumerate(strategy.iter_targets(limit)):
                address = targets.address(c64)
                manifest_writer.write({
                    "probe_id": f"{method}:{round_name}:{index}",
                    "node": node, "mode": mode,
                    "c64": str(ipaddress.ip_network((c64 << 64, 64))),
                    "target_ipv6": address,
                })
                sf.write(address + "\n")
                stage_counts[mode] += 1
                count += 1
        manifest_writer.close()

        log_memory(f"after generating batch {batch_number}")

        if count == 0:
            # Candidates exhausted; nothing was sent, so drop the empty batch.
            shutil.rmtree(folder)
            batch_number -= 1
            break

        write_checkpoint_atomic(
            folder / "prepared-state.pkl.gz",
            checkpoint_payload(batch_context, batch_number),
        )
        log_memory(f"after checkpointing batch {batch_number}")

        command = [
            "bash", str(run_scan), str(zmap), str(sent), str(raw),
            scanner["source_ipv6"], scanner["interface"], scanner["gateway_mac"],
            str(scanner["rate_pps"]), str(scanner["cooldown_seconds"]),
        ]
        with open(folder / "scan.log", "w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (folder / "scan-complete").touch()
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
        fh.writelines(str(ipaddress.IPv6Address(router)) + "\n" for router in sorted(last_hop_routers))
    finished_utc = datetime.now(timezone.utc)
    runtime_seconds = None
    if batch_context.started_utc:
        runtime_seconds = (
            finished_utc - datetime.fromisoformat(batch_context.started_utc)
        ).total_seconds()
    summary = {
        "method": method,
        "started_from": "RIS BGP only",
        "started_utc": batch_context.started_utc,
        "finished_utc": finished_utc.isoformat(),
        "runtime_seconds": runtime_seconds,
        "budget_total": allowance,
        "formal_sent": strategy.sent,
        "budget_exhausted": strategy.sent == allowance,
        "distinct_new_positive_c64": strategy.positive_count,
        "distinct_last_hop_router_addresses": len(last_hop_routers),
        "stages": dict(stage_counts),
        "native_prefix_count": len(native),
    }
    if hasattr(strategy, "native_positive_count"):
        summary["native_observed_positive_c64"] = strategy.native_positive_count
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
