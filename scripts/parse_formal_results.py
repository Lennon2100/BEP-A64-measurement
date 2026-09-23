#!/usr/bin/env python3
"""Stream one formal batch into compact probe evidence and node feedback."""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

from parse_results import RAW_FIELDS, canonical_ipv6, parse_int, response_class, timing_ms


PROBE_FIELDS = [
    "probe_id", "node", "mode", "c64", "target_ipv6", "match_status",
    "response_class", "is_observed_positive", "icmp_source", "rtt_ms",
    "raw_row_number",
]
ROUTER_FIELDS = [
    "probe_id", "c64", "target_ipv6", "router_ipv6", "response_class",
    "rtt_ms", "raw_row_number",
]
FEEDBACK_FIELDS = ["node", "mode", "probes", "positives", "replies", "sources"]


def load_raw(path, threshold):
    responses = defaultdict(list)
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
                target = canonical_ipv6(row["orig-dest-ip"])
                icmp_type = parse_int(row, "type")
                icmp_code = parse_int(row, "code")
                rtt = timing_ms(row)
                cls, positive = response_class(icmp_type, icmp_code, rtt, threshold)
                source = canonical_ipv6(row["saddr"]) if row.get("saddr") else ""
            except ValueError:
                invalid += 1
                continue
            responses[target].append({
                "class": cls, "positive": positive, "source": source,
                "rtt": f"{rtt:.3f}", "elapsed": rtt, "line": line_number,
                "is_au": icmp_type == 1 and icmp_code == 3,
            })
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
        feedback = defaultdict(lambda: {"probes": 0, "positives": 0, "replies": 0, "sources": set()})
        statuses = Counter()
        classes = Counter()
        planned = positives = multi = cross_class = 0
        with open(args.manifest, newline="", encoding="utf-8") as mf, \
             open(args.probes, "w", newline="", encoding="utf-8") as pf, \
             open(args.routers, "w", newline="", encoding="utf-8") as rf:
            reader = csv.DictReader(mf)
            required = {"probe_id", "node", "mode", "c64", "target_ipv6"}
            if required - set(reader.fieldnames or ()):
                raise ValueError("formal manifest is missing required fields")
            probe_writer = csv.DictWriter(pf, fieldnames=PROBE_FIELDS)
            router_writer = csv.DictWriter(rf, fieldnames=ROUTER_FIELDS)
            probe_writer.writeheader()
            router_writer.writeheader()
            for row in reader:
                planned += 1
                target = canonical_ipv6(row["target_ipv6"])
                found = responses.pop(target, [])
                if found:
                    response_classes = {item["class"] for item in found}
                    cross_class += len(response_classes) > 1
                    # One sent target is one budget item.  Prefer the first
                    # positive arrival when any response satisfies the IMC
                    # rule; otherwise retain the first arrival.  The raw CSV
                    # remains the complete multi-response evidence.
                    positive_responses = [candidate for candidate in found if candidate["positive"]]
                    item = min(positive_responses or found, key=lambda candidate: candidate["elapsed"])
                    multi += len(found) > 1
                    status = "matched"
                    cls = item["class"]
                    positive = int(any(candidate["positive"] for candidate in found))
                else:
                    item = {"source": "", "rtt": "", "line": "", "is_au": False}
                    status, cls, positive = "timeout", "timeout", 0
                key = (row["node"], row["mode"])
                aggregate = feedback[key]
                aggregate["probes"] += 1
                aggregate["positives"] += positive
                aggregate["replies"] += any(
                    candidate["class"] in ("direct", "slow_au", "fast_au")
                    for candidate in found
                )
                for candidate in found:
                    if candidate["is_au"] and candidate["source"]:
                        aggregate["sources"].add(candidate["source"])
                        router_writer.writerow({
                            "probe_id": row["probe_id"], "c64": row["c64"],
                            "target_ipv6": target, "router_ipv6": candidate["source"],
                            "response_class": candidate["class"], "rtt_ms": candidate["rtt"],
                            "raw_row_number": candidate["line"],
                        })
                positives += positive
                statuses[status] += 1
                classes[cls] += 1
                probe_writer.writerow({
                    "probe_id": row["probe_id"], "node": row["node"],
                    "mode": row["mode"], "c64": row["c64"],
                    "target_ipv6": target, "match_status": status,
                    "response_class": cls, "is_observed_positive": positive,
                    "icmp_source": item["source"], "rtt_ms": item["rtt"],
                    "raw_row_number": item["line"],
                })

        with open(args.feedback, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FEEDBACK_FIELDS)
            writer.writeheader()
            for (node, mode), item in feedback.items():
                writer.writerow({
                    "node": node, "mode": mode, "probes": item["probes"],
                    "positives": item["positives"], "replies": item["replies"],
                    "sources": json.dumps(sorted(item["sources"])),
                })
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
