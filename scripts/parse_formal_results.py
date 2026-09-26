#!/usr/bin/env python3
"""Stream one scan batch into compact probe evidence and strategy feedback."""

import argparse
import csv
import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from parquet_io import StringParquetWriter, iter_table_file


RAW_FIELDS = [
    "orig-dest-ip", "classification", "success", "type", "code", "saddr",
    "ttl", "original_ttl", "sent_timestamp_ts", "sent_timestamp_us", "nrsent",
    "timestamp_str", "timestamp_ts", "timestamp_us",
]


def parse_int(row, field):
    text = row[field].strip()
    if not text:
        raise ValueError(f"empty {field}")
    return int(text)


def timing_ms(row):
    sent_us = parse_int(row, "sent_timestamp_ts") * 1_000_000 + parse_int(
        row, "sent_timestamp_us"
    )
    recv_us = parse_int(row, "timestamp_ts") * 1_000_000 + parse_int(
        row, "timestamp_us"
    )
    elapsed_us = recv_us - sent_us
    if elapsed_us < 0:
        raise ValueError("receive timestamp precedes send timestamp")
    return elapsed_us / 1000


def response_class(icmp_type, icmp_code, rtt_ms, slow_au_threshold_ms):
    if icmp_type == 129:
        return "direct", 1
    if icmp_type == 1:
        if icmp_code == 3:
            if rtt_ms >= slow_au_threshold_ms:
                return "slow_au", 1
            return "fast_au", 0
        if icmp_code == 0:
            return "nr", 0
        if icmp_code == 1:
            return "ap", 0
        if icmp_code == 6:
            return "rr", 0
        return "other_error", 0
    if icmp_type == 3:
        return "tx", 0
    return "other_error", 0


PROBE_FIELDS = [
    "probe_id", "node", "mode", "c64", "target_ipv6", "match_status",
    "response_class", "is_observed_positive", "icmp_source", "rtt_ms",
    "raw_row_number",
]
ROUTER_FIELDS = [
    "probe_id", "c64", "target_ipv6", "router_ipv6", "response_class",
    "rtt_ms", "raw_row_number",
]
FEEDBACK_FIELDS = [
    "node", "mode", "probes", "positives", "replies", "sources",
    "bep_active", "bep_inactive", "bep_null",
]

CLASS_BITS = {
    "direct": 1,
    "slow_au": 2,
    "fast_au": 4,
    "nr": 8,
    "ap": 16,
    "rr": 32,
    "tx": 64,
    "other_error": 128,
}
REPLY_CLASSES = {"direct", "slow_au", "fast_au"}


@dataclass(slots=True)
class ResponseAggregate:
    count: int = 0
    class_mask: int = 0
    positive: bool = False
    reply: bool = False
    representative: tuple | None = None
    au_sources: dict | None = None

    def add(self, cls, positive, observation):
        source, rtt, line, is_au = observation
        self.count += 1
        self.class_mask |= CLASS_BITS[cls]
        self.reply |= cls in REPLY_CLASSES
        candidate = (rtt, cls, source, line)
        if positive:
            if not self.positive or rtt < self.representative[0]:
                self.representative = candidate
            self.positive = True
        elif not self.positive and (
            self.representative is None or rtt < self.representative[0]
        ):
            self.representative = candidate
        if is_au and source is not None:
            if self.au_sources is None:
                self.au_sources = {}
            previous = self.au_sources.get(source)
            if previous is None or rtt < previous[0]:
                self.au_sources[source] = (rtt, cls, line)


def ipv6_int(text):
    address = ipaddress.ip_address(text.strip())
    if address.version != 6:
        raise ValueError(f"not an IPv6 address: {text}")
    return int(address)


def load_raw(path, threshold):
    responses = {}
    invalid = 0
    rows = 0
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = set(RAW_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"raw ZMap CSV is missing fields: {sorted(missing)}")
        for line_number, row in enumerate(reader, 2):
            rows += 1
            try:
                target = ipv6_int(row["orig-dest-ip"])
                icmp_type = parse_int(row, "type")
                icmp_code = parse_int(row, "code")
                rtt = timing_ms(row)
                cls, positive = response_class(icmp_type, icmp_code, rtt, threshold)
                source = ipv6_int(row["saddr"]) if row.get("saddr") else None
            except ValueError:
                invalid += 1
                continue
            aggregate = responses.get(target)
            if aggregate is None:
                aggregate = responses[target] = ResponseAggregate()
            observation = (source, rtt, line_number, icmp_type == 1 and icmp_code == 3)
            aggregate.add(cls, positive, observation)
    return responses, rows, invalid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("raw")
    parser.add_argument("probes")
    parser.add_argument("feedback")
    parser.add_argument("routers")
    parser.add_argument("--slow-au-threshold-ms", type=int, default=1000)
    args = parser.parse_args(argv)
    outputs = (args.probes, args.feedback, args.routers, args.probes + ".summary.json")
    if any(os.path.exists(path) for path in outputs):
        parser.error("refusing to overwrite formal parser output")

    try:
        responses, raw_count, invalid_count = load_raw(args.raw, args.slow_au_threshold_ms)
        feedback = defaultdict(lambda: {
            "probes": 0, "positives": 0, "replies": 0, "sources": set(),
            "bep_active": 0, "bep_inactive": 0, "bep_null": 0,
        })
        statuses = Counter()
        classes = Counter()
        planned = positives = multi = cross_class = 0
        probe_writer = StringParquetWriter(args.probes, PROBE_FIELDS)
        router_writer = StringParquetWriter(args.routers, ROUTER_FIELDS)
        manifest_path = Path(args.manifest)
        for row in iter_table_file(manifest_path, manifest_path.with_suffix(".csv")):
            planned += 1
            target_number = ipv6_int(row["target_ipv6"])
            target = str(ipaddress.IPv6Address(target_number))
            found = responses.pop(target_number, None)
            if found:
                cross_class += found.class_mask.bit_count() > 1
                # One sent target is one budget item.  Prefer the first
                # positive arrival when any response satisfies the IMC
                # rule; otherwise retain the first arrival.  The raw CSV
                # remains the complete multi-response evidence.
                rtt, cls, source, raw_line = found.representative
                multi += found.count > 1
                status = "matched"
                positive = int(found.positive)
            else:
                source = rtt = raw_line = None
                status, cls, positive = "timeout", "timeout", 0
            key = (row["node"], row["mode"])
            aggregate = feedback[key]
            aggregate["probes"] += 1
            aggregate["positives"] += positive
            aggregate["replies"] += bool(found and found.reply)
            if found and found.class_mask & CLASS_BITS["slow_au"]:
                aggregate["bep_active"] += 1
            elif found and found.class_mask & (
                CLASS_BITS["fast_au"] | CLASS_BITS["tx"] | CLASS_BITS["rr"]
            ):
                aggregate["bep_inactive"] += 1
            else:
                aggregate["bep_null"] += 1
            for router, observation in (found.au_sources or {}).items() if found else ():
                router_text = str(ipaddress.IPv6Address(router))
                router_rtt, router_class, router_line = observation
                # TNet consumes candidate-screen router evidence from the
                # streamed observation file. Avoid duplicating millions of
                # one-shot router values in its node feedback aggregate.
                if (
                    row["mode"] != "candidate_screen"
                    and not row["mode"].startswith("conference_")
                ):
                    aggregate["sources"].add(router_text)
                router_writer.write({
                    "probe_id": row["probe_id"], "c64": row["c64"],
                    "target_ipv6": target, "router_ipv6": router_text,
                    "response_class": router_class,
                    "rtt_ms": f"{router_rtt:.3f}",
                    "raw_row_number": router_line,
                })
            positives += positive
            statuses[status] += 1
            classes[cls] += 1
            probe_writer.write({
                "probe_id": row["probe_id"], "node": row["node"],
                "mode": row["mode"], "c64": row["c64"],
                "target_ipv6": target, "match_status": status,
                "response_class": cls, "is_observed_positive": positive,
                "icmp_source": str(ipaddress.IPv6Address(source)) if source is not None else "",
                "rtt_ms": f"{rtt:.3f}" if rtt is not None else "",
                "raw_row_number": raw_line if raw_line is not None else "",
            })
        probe_writer.close()
        router_writer.close()

        feedback_writer = StringParquetWriter(args.feedback, FEEDBACK_FIELDS)
        for (node, mode), item in feedback.items():
            feedback_writer.write({
                "node": node, "mode": mode, "probes": item["probes"],
                "positives": item["positives"], "replies": item["replies"],
                "sources": json.dumps(sorted(item["sources"])),
                "bep_active": item["bep_active"],
                "bep_inactive": item["bep_inactive"],
                "bep_null": item["bep_null"],
            })
        feedback_writer.close()
        summary = {
            "planned_probe_count": planned, "raw_response_count": raw_count,
            "invalid_raw_row_count": invalid_count,
            "unmatched_raw_target_count": len(responses),
            "multi_response_target_count": multi,
            "cross_class_target_count": cross_class,
            "observed_positive_count": positives,
            "match_status_counts": dict(statuses),
            "response_class_counts": dict(classes),
        }
        with open(args.probes + ".summary.json", "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
