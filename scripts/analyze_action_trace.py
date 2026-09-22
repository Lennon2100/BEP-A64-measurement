#!/usr/bin/env python3
"""Analyze one formal calibration action trace (D054-D056).

Reads the action-trace sidecar produced by prepare_action_trace.py and one raw
ZMap CSV, matches responses back to targets with the same per-probe parser
contract as parse_results.py, then reports the horizon-by-horizon yield of each
action, the within-root paired comparison of `split-d` against `sample`, and the
achieved detection sensitivity at every horizon.

The cheap observation is one search IID per C64: `new_distinct_a64 = 1` exactly
when the matched response is `direct` or `slow_au` (the D052-validated guide).
The reference panel is deliberately not repeated per child; the 92.5% recall
applies uniformly across arms and does not bias the arm ranking.

D056 detection sensitivity is reported alongside every achieved horizon `k`:
`p_detect(k, delta) = 1 - delta**(1/k)` is the density above which `k`
no-positive probes give a (1-delta) detection guarantee.  It is a strength label
for each no-positive observation, not an allocation command; a coarse root is
not forced to reach an infeasible `q(v; p*, delta)`.

Outputs:

  action_trace_probes.csv   the sidecar joined to per-probe observations;
  action_trace_summary.json horizon yields, paired sample-vs-split, and
                            per-root yield with prefix length/C64 capacity,
                            achieved k, and p_detect.
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

import parse_results as pr

HORIZONS = (1, 2, 4, 8, 16, 32)

PROBE_OUTPUT_FIELDS = [
    "probe_id",
    "action_id",
    "parent_prefix",
    "prefix_length",
    "c64_capacity",
    "d",
    "probe_order",
    "selected_child",
    "target_c64",
    "target_ipv6",
    "iid",
    "root_stratum",
    "probe_cost",
    "match_status",
    "response_class",
    "is_observed_positive",
    "new_distinct_a64",
    "rtt_ms",
    "icmp_source",
]


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def grouped_by(items, keyfn):
    grouped = defaultdict(list)
    for item in items:
        grouped[keyfn(item)].append(item)
    return grouped


def p_detect(k, delta):
    """Density above which `k` no-positive probes guarantee (1-delta) detection.

    D056: `p_detect(k, delta) = 1 - delta**(1/k)`.  Returns None for k <= 0.
    """
    if k <= 0:
        return None
    return 1.0 - delta ** (1.0 / k)


def timeout_row(row):
    out = dict(row)
    out.update(
        {
            "match_status": "timeout",
            "response_class": "timeout",
            "is_observed_positive": "0",
            "new_distinct_a64": "0",
            "rtt_ms": "",
            "icmp_source": "",
        }
    )
    return out


def matched_row(row, obs):
    out = dict(row)
    out.update(
        {
            "match_status": obs["match_status"],
            "response_class": obs["response_class"],
            "is_observed_positive": str(obs["is_observed_positive"]),
            "new_distinct_a64": (
                "1" if obs["response_class"] in ("direct", "slow_au") else "0"
            ),
            "rtt_ms": obs["rtt_ms"],
            "icmp_source": obs.get("icmp_source", ""),
        }
    )
    return out


def horizon_yield(probes):
    """Cumulative/marginal new-A64 counts at each feasible horizon.

    `probes` is one action's per-probe rows ordered by `probe_order`.  Horizons
    larger than the action's effective length are omitted.
    """
    effective = len(probes)
    result = []
    previous = 0
    for h in HORIZONS:
        if h > effective:
            break
        cumulative = sum(int(row["new_distinct_a64"]) for row in probes[:h])
        result.append(
            {
                "horizon": h,
                "cumulative_new_a64": cumulative,
                "marginal_new_a64": cumulative - previous,
            }
        )
        previous = cumulative
    return result, effective


def aggregate_yield(action_rows, group_keys, delta):
    """Aggregate per-root horizon rows by a tuple of grouping fields."""
    grouped = grouped_by(action_rows, lambda r: tuple(r[k] for k in group_keys))
    out = []
    for key in sorted(grouped):
        totals = defaultdict(int)
        counts = defaultdict(int)
        for item in grouped[key]:
            totals[item["horizon"]] += item["cumulative_new_a64"]
            counts[item["horizon"]] += 1
        for h in sorted(totals):
            out.append(
                {
                    **{k: v for k, v in zip(group_keys, key)},
                    "horizon": h,
                    "root_count": counts[h],
                    "total_new_a64": totals[h],
                    "mean_new_a64_per_root": totals[h] / counts[h],
                    "positive_rate": totals[h] / (counts[h] * h),
                    "p_detect": p_detect(h, delta),
                }
            )
    return out


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar_csv")
    parser.add_argument("raw_zmap_csv")
    parser.add_argument("output_dir")
    parser.add_argument("--slow-au-threshold-ms", type=int, default=1000)
    parser.add_argument("--delta", type=float, default=0.05)
    args = parser.parse_args(argv)

    if args.slow_au_threshold_ms <= 0:
        parser.error("--slow-au-threshold-ms must be positive")
    if not 0 < args.delta < 1:
        parser.error("--delta must be between 0 and 1")

    try:
        sidecar = load_csv(args.sidecar_csv)
        plan = {row["target_ipv6"]: row for row in sidecar}
        if len(plan) != len(sidecar):
            raise ValueError("sidecar contains duplicate target_ipv6 rows")

        observations, unmatched, multi_response = pr.load_raw(
            args.raw_zmap_csv, plan, args.slow_au_threshold_ms
        )

        output_rows = []
        for row in sidecar:
            obs = observations.get(row["target_ipv6"])
            output_rows.append(
                matched_row(row, obs) if obs is not None else timeout_row(row)
            )

        os.makedirs(args.output_dir, exist_ok=True)
        probes_path = os.path.join(args.output_dir, "action_trace_probes.csv")
        summary_path = os.path.join(args.output_dir, "action_trace_summary.json")
        if os.path.exists(summary_path):
            raise FileExistsError(
                f"refusing to overwrite existing output: {summary_path}"
            )
        with open(probes_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=PROBE_OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(output_rows)

        # ---- per-action ordered sequences -----------------------------------
        by_action = defaultdict(list)
        for row in output_rows:
            by_action[(row["parent_prefix"], row["action_id"])].append(row)
        for key in by_action:
            by_action[key].sort(key=lambda r: int(r["probe_order"]))

        action_effective = {}
        action_rows = []
        for (root, action_id), rows in by_action.items():
            yields, effective = horizon_yield(rows)
            action_effective[(root, action_id)] = effective
            stratum = rows[0]["root_stratum"]
            prefix_length = rows[0]["prefix_length"]
            c64_capacity = rows[0]["c64_capacity"]
            for item in yields:
                action_rows.append(
                    {
                        "root_prefix": root,
                        "root_stratum": stratum,
                        "prefix_length": prefix_length,
                        "c64_capacity": c64_capacity,
                        "action_id": action_id,
                        "d": rows[0]["d"],
                        "effective_k": effective,
                        "horizon": item["horizon"],
                        "cumulative_new_a64": item["cumulative_new_a64"],
                        "marginal_new_a64": item["marginal_new_a64"],
                        "p_detect": p_detect(item["horizon"], args.delta),
                    }
                )

        yield_overall = aggregate_yield(action_rows, ["action_id"], args.delta)
        yield_by_stratum = aggregate_yield(
            action_rows, ["action_id", "root_stratum"], args.delta
        )

        # ---- paired sample vs split-d, per horizon --------------------------
        def cumulative_at(root, action_id, h):
            key = (root, action_id)
            rows = by_action.get(key)
            if rows is None or h > action_effective.get(key, 0):
                return None
            return sum(int(r["new_distinct_a64"]) for r in rows[:h])

        paired_rows = []
        for root in sorted({r for (r, _) in by_action}):
            for d in ("1", "4", "8"):
                split_id = f"split-{d}"
                if (root, split_id) not in by_action:
                    continue
                for h in HORIZONS:
                    sample = cumulative_at(root, "sample", h)
                    split = cumulative_at(root, split_id, h)
                    if sample is None or split is None:
                        continue
                    paired_rows.append(
                        {
                            "root_prefix": root,
                            "root_stratum": by_action[(root, "sample")][0][
                                "root_stratum"
                            ],
                            "d": int(d),
                            "horizon": h,
                            "sample_new_a64": sample,
                            "split_new_a64": split,
                            "diff": split - sample,
                        }
                    )

        paired_summary = []
        for (d, h), items in sorted(
            grouped_by(paired_rows, lambda r: (r["d"], r["horizon"])).items()
        ):
            diffs = [r["diff"] for r in items]
            paired_summary.append(
                {
                    "d": d,
                    "horizon": h,
                    "root_count": len(items),
                    "mean_diff": sum(diffs) / len(diffs),
                    "split_wins": sum(1 for x in diffs if x > 0),
                    "split_loses": sum(1 for x in diffs if x < 0),
                    "ties": sum(1 for x in diffs if x == 0),
                }
            )

        status_counts = Counter(row["match_status"] for row in output_rows)
        class_counts = Counter(row["response_class"] for row in output_rows)

        summary = {
            "sidecar_csv": os.path.abspath(args.sidecar_csv),
            "raw_zmap_csv": os.path.abspath(args.raw_zmap_csv),
            "output_dir": os.path.abspath(args.output_dir),
            "slow_au_threshold_ms": args.slow_au_threshold_ms,
            "delta": args.delta,
            "validation": {
                "planned_probe_count": len(sidecar),
                "matched_count": status_counts.get("matched", 0),
                "timeout_count": status_counts.get("timeout", 0),
                "unmatched_raw_count": len(unmatched),
                "multi_response_target_count": len(multi_response),
                "match_status_counts": dict(status_counts),
                "response_class_counts": dict(class_counts),
                "new_distinct_a64_count": sum(
                    int(row["new_distinct_a64"]) for row in output_rows
                ),
            },
            "yield_overall": yield_overall,
            "yield_by_stratum": yield_by_stratum,
            "root_yield": action_rows,
            "paired_sample_vs_split": paired_summary,
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
