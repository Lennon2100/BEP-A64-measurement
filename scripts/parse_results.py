#!/usr/bin/env python3
"""Join one ZMap round to its target manifest and derive ICMP observations."""

import argparse
import csv
import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict


PLAN_FIELDS = [
    "probe_id",
    "panel_id",
    "tranche",
    "root_stratum",
    "root_prefix",
    "selection_arm",
    "c64",
    "target_ipv6",
    "role",
    "iid_index",
    "round",
]

RAW_FIELDS = [
    "orig-dest-ip",
    "classification",
    "success",
    "type",
    "code",
    "saddr",
    "ttl",
    "original_ttl",
    "sent_timestamp_ts",
    "sent_timestamp_us",
    "nrsent",
    "timestamp_str",
    "timestamp_ts",
    "timestamp_us",
]

OUTPUT_FIELDS = PLAN_FIELDS + [
    "match_status",
    "response_class",
    "is_observed_positive",
    "slow_au_threshold_ms",
    "zmap_classification",
    "zmap_success",
    "icmp_type",
    "icmp_code",
    "icmp_source",
    "ttl",
    "original_ttl",
    "quoted_target",
    "sent_timestamp_ts",
    "sent_timestamp_us",
    "nrsent",
    "recv_timestamp_str",
    "recv_timestamp_ts",
    "recv_timestamp_us",
    "rtt_ms",
    "raw_row_number",
]

ROUTER_FIELDS = [
    "probe_id", "c64", "target_ipv6", "router_ipv6", "response_class",
    "rtt_ms", "raw_row_number",
]


def canonical_ipv6(text):
    address = ipaddress.ip_address(text.strip())
    if address.version != 6:
        raise ValueError(f"not an IPv6 address: {text!r}")
    return str(address)


def load_plan(path, round_name):
    selected = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = set(PLAN_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"target manifest is missing fields: {sorted(missing)}")
        for line_number, row in enumerate(reader, 2):
            if row["round"] != round_name:
                continue
            try:
                target = canonical_ipv6(row["target_ipv6"])
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if target in selected:
                raise ValueError(
                    f"{path}:{line_number}: duplicate target in round {round_name}: "
                    f"{target}"
                )
            normalized = {field: row[field] for field in PLAN_FIELDS}
            normalized["target_ipv6"] = target
            selected[target] = normalized
    if not selected:
        raise ValueError(f"target manifest contains no rows for round {round_name!r}")
    return selected


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


def raw_output_fields(row):
    return {
        "zmap_classification": row.get("classification", ""),
        "zmap_success": row.get("success", ""),
        "icmp_type": row.get("type", ""),
        "icmp_code": row.get("code", ""),
        "icmp_source": row.get("saddr", ""),
        "ttl": row.get("ttl", ""),
        "original_ttl": row.get("original_ttl", ""),
        "quoted_target": row.get("orig-dest-ip", ""),
        "sent_timestamp_ts": row.get("sent_timestamp_ts", ""),
        "sent_timestamp_us": row.get("sent_timestamp_us", ""),
        "nrsent": row.get("nrsent", ""),
        "recv_timestamp_str": row.get("timestamp_str", ""),
        "recv_timestamp_ts": row.get("timestamp_ts", ""),
        "recv_timestamp_us": row.get("timestamp_us", ""),
    }


def empty_plan_row():
    return {field: "" for field in PLAN_FIELDS}


def unmatched_row(plan_row, raw_row, raw_row_number, threshold):
    output = dict(plan_row)
    output.update(
        {
            "match_status": "unmatched",
            "response_class": "unmatched",
            "is_observed_positive": 0,
            "slow_au_threshold_ms": threshold,
            "rtt_ms": "",
            "raw_row_number": raw_row_number,
        }
    )
    output.update(raw_output_fields(raw_row))
    return output


def load_raw(path, plan, slow_au_threshold_ms, router_rows=None):
    responses = defaultdict(list)
    unmatched = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = set(RAW_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"raw ZMap CSV is missing fields: {sorted(missing)}")
        for line_number, row in enumerate(reader, 2):
            try:
                target = canonical_ipv6(row["orig-dest-ip"])
            except ValueError:
                unmatched.append(
                    unmatched_row(
                        empty_plan_row(), row, line_number, slow_au_threshold_ms
                    )
                )
                continue

            plan_row = plan.get(target)
            if plan_row is None:
                unmatched.append(
                    unmatched_row(
                        empty_plan_row(), row, line_number, slow_au_threshold_ms
                    )
                )
                continue

            try:
                icmp_type = parse_int(row, "type")
                icmp_code = parse_int(row, "code")
                rtt_ms = timing_ms(row)
            except ValueError:
                responses[target].append(
                    unmatched_row(plan_row, row, line_number, slow_au_threshold_ms)
                )
                continue

            derived_class, is_positive = response_class(
                icmp_type, icmp_code, rtt_ms, slow_au_threshold_ms
            )
            output = dict(plan_row)
            output.update(
                {
                    "match_status": "matched",
                    "response_class": derived_class,
                    "is_observed_positive": is_positive,
                    "slow_au_threshold_ms": slow_au_threshold_ms,
                    "rtt_ms": f"{rtt_ms:.3f}",
                    "raw_row_number": line_number,
                }
            )
            output.update(raw_output_fields(row))
            output["quoted_target"] = target
            responses[target].append(output)
            if router_rows is not None and icmp_type == 1 and icmp_code == 3 and row.get("saddr"):
                router_rows.append({
                    "probe_id": plan_row["probe_id"],
                    "c64": plan_row["c64"],
                    "target_ipv6": target,
                    "router_ipv6": canonical_ipv6(row["saddr"]),
                    "response_class": derived_class,
                    "rtt_ms": output["rtt_ms"],
                    "raw_row_number": line_number,
                })

    observations = {}
    multi_response = []
    for target, rows in responses.items():
        if len(rows) == 1:
            observations[target] = rows[0]
            continue
        classes = {row["response_class"] for row in rows}
        if len(classes) != 1:
            raise ValueError(
                f"{path}: multiple responses for target {target} span classes "
                f"{sorted(classes)}; one-row probe selection is undefined"
            )
        # A single probe in a routing loop elicits several same-class responses
        # from different routers. Keep the first arrival and report the
        # multiplicity separately; the raw CSV retains every row.
        observations[target] = rows[0]
        multi_response.append(
            {
                "target": target,
                "response_count": len(rows),
                "response_class": classes.pop(),
            }
        )
    return observations, unmatched, multi_response


def timeout_row(plan_row, threshold):
    output = dict(plan_row)
    output.update({field: "" for field in OUTPUT_FIELDS if field not in output})
    output.update(
        {
            "match_status": "timeout",
            "response_class": "timeout",
            "is_observed_positive": 0,
            "slow_au_threshold_ms": threshold,
        }
    )
    return output


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_manifest")
    parser.add_argument("round")
    parser.add_argument("raw_zmap_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--slow-au-threshold-ms", type=int, default=1000)
    parser.add_argument("--last-hop-router-output")
    args = parser.parse_args(argv)

    if args.slow_au_threshold_ms <= 0:
        parser.error("--slow-au-threshold-ms must be positive")
    summary_path = f"{args.output_csv}.summary.json"
    for output in (args.output_csv, summary_path, args.last_hop_router_output):
        if output is None:
            continue
        if os.path.exists(output):
            print(f"refusing to overwrite existing output: {output}", file=sys.stderr)
            return 1

    try:
        plan = load_plan(args.target_manifest, args.round)
        router_rows = [] if args.last_hop_router_output else None
        observations, unmatched, multi_response = load_raw(
            args.raw_zmap_csv, plan, args.slow_au_threshold_ms, router_rows,
        )
        output_rows = [
            observations.get(target, timeout_row(plan_row, args.slow_au_threshold_ms))
            for target, plan_row in plan.items()
        ]
        output_rows.extend(unmatched)

        output_dir = os.path.dirname(os.path.abspath(args.output_csv))
        os.makedirs(output_dir, exist_ok=True)
        with open(args.output_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(output_rows)

        if args.last_hop_router_output:
            with open(args.last_hop_router_output, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=ROUTER_FIELDS)
                writer.writeheader()
                writer.writerows(router_rows)

        status_counts = Counter(row["match_status"] for row in output_rows)
        class_counts = Counter(row["response_class"] for row in output_rows)
        summary = {
            "target_manifest": os.path.abspath(args.target_manifest),
            "round": args.round,
            "raw_zmap_csv": os.path.abspath(args.raw_zmap_csv),
            "output_csv": os.path.abspath(args.output_csv),
            "slow_au_threshold_ms": args.slow_au_threshold_ms,
            "planned_probe_count": len(plan),
            "raw_response_count": (
                len(observations)
                + len(unmatched)
                + sum(item["response_count"] - 1 for item in multi_response)
            ),
            "multi_response_target_count": len(multi_response),
            "multi_response_targets": multi_response,
            "output_row_count": len(output_rows),
            "match_status_counts": dict(status_counts),
            "response_class_counts": dict(class_counts),
            "observed_positive_count": sum(
                int(row["is_observed_positive"]) for row in output_rows
            ),
        }
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
