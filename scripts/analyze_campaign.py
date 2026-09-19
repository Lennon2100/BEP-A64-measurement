#!/usr/bin/env python3
"""Calibration-only analyzer for the D050 six-round panel.

Reads the effective manifest, the six parsed probe rounds, and the calibration
unit table, checks the round/panel structure, then writes one per-panel label
table plus the six calibration summaries. It does not implement the journal
policy.
"""

import argparse
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

REFERENCE_IIDS = 5

LABEL_FIELDS = [
    "panel_id",
    "root_prefix",
    "root_stratum",
    "selection_arm",
    "c64",
    "search_response_class",
    "search_positive",
    "reference_positive_m1",
    "reference_positive_m2",
    "reference_positive_m3",
    "reference_positive_m4",
    "reference_positive_m5",
    "first_positive_reference_index",
    "reference_response_pattern",
    "icmp_source_count",
]


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def positive_at_threshold(probe, threshold_ms):
    """Recompute one probe's positive flag at a different slow-AU cutoff."""
    cls = probe["response_class"]
    if cls == "direct":
        return True
    if cls in ("slow_au", "fast_au"):
        return float(probe["rtt_ms"]) >= threshold_ms
    return False


def rtt_stats(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": min(ordered),
        "max": max(ordered),
        "mean": statistics.mean(ordered),
        "median": statistics.median(ordered),
    }


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_json")
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    cfg = load_json(args.campaign_json)
    rounds = cfg["sampling"]["rounds"]
    output_root = cfg["output_root"]
    thresholds = cfg["labels"]["threshold_sensitivity_ms"]
    main_threshold = cfg["labels"]["slow_au_threshold_ms"]

    manifest_path = os.path.join(output_root, "effective_calibration_targets.csv")
    units_path = os.path.join(
        os.path.dirname(cfg["input"]["target_manifest"]), "calibration_units.csv"
    )
    probe_paths = {
        r: os.path.join(output_root, r, f"probes-{r}.csv") for r in rounds
    }

    manifest = load_csv(manifest_path)
    units = load_csv(units_path)
    probes_by_round = {r: load_csv(probe_paths[r]) for r in rounds}

    out_dir = args.output_dir or os.path.join(output_root, "analysis")
    os.makedirs(out_dir, exist_ok=True)

    # ---- structural validation --------------------------------------------
    probe_by_key = {}
    panel_sets = {r: set() for r in rounds}
    probe_ids = set()
    duplicate_probe_ids = []
    status_counts = Counter()
    for r, rows in probes_by_round.items():
        for row in rows:
            key = (row["panel_id"], row["role"], row["iid_index"])
            if key in probe_by_key:
                raise ValueError(f"duplicate probe key {key}")
            probe_by_key[key] = row
            panel_sets[r].add(row["panel_id"])
            if row["probe_id"] in probe_ids:
                duplicate_probe_ids.append(row["probe_id"])
            probe_ids.add(row["probe_id"])
            status_counts[row["match_status"]] += 1

    reference_panel_set = panel_sets[rounds[0]]
    panels_consistent = all(panel_sets[r] == reference_panel_set for r in rounds)
    panel_count = len(reference_panel_set)

    panels_probe_count = Counter(key[0] for key in probe_by_key)
    malformed_panels = {
        panel: count
        for panel, count in panels_probe_count.items()
        if count != 1 + REFERENCE_IIDS
    }

    planned_targets = {row["target_ipv6"] for row in manifest}
    probed_targets = {
        row["target_ipv6"]
        for rows in probes_by_round.values()
        for row in rows
        if row["target_ipv6"]
    }

    unmatched_by_round = {
        r: sum(1 for row in rows if row["match_status"] == "unmatched")
        for r, rows in probes_by_round.items()
    }
    multi_response_by_round = {}
    for r in rounds:
        summary_path = f"{probe_paths[r]}.summary.json"
        multi_response_by_round[r] = load_json(summary_path).get(
            "multi_response_target_count", 0
        )

    # ---- build per-panel labels -------------------------------------------
    units_by_panel = {row["panel_id"]: row for row in units}

    panels = []
    for panel_id in sorted(reference_panel_set):
        search = probe_by_key[(panel_id, "search", "0")]
        refs = [
            probe_by_key[(panel_id, "reference", str(i))]
            for i in range(1, REFERENCE_IIDS + 1)
        ]
        unit = units_by_panel.get(panel_id, search)
        ref_pos = [int(row["is_observed_positive"]) for row in refs]
        cumulative = [any(ref_pos[:m]) for m in range(1, REFERENCE_IIDS + 1)]
        first_positive = next(
            (i for i in range(1, REFERENCE_IIDS + 1) if ref_pos[i - 1]), ""
        )
        sources = {
            row["icmp_source"] for row in [search] + refs if row["icmp_source"]
        }
        panels.append(
            {
                "panel_id": panel_id,
                "root_prefix": unit["root_prefix"],
                "root_stratum": unit["root_stratum"],
                "selection_arm": unit["selection_arm"],
                "c64": unit["c64"],
                "search_response_class": search["response_class"],
                "search_positive": int(search["is_observed_positive"]),
                "reference_positive_m1": int(cumulative[0]),
                "reference_positive_m2": int(cumulative[1]),
                "reference_positive_m3": int(cumulative[2]),
                "reference_positive_m4": int(cumulative[3]),
                "reference_positive_m5": int(cumulative[4]),
                "first_positive_reference_index": first_positive,
                "reference_response_pattern": "".join("1" if p else "0" for p in ref_pos),
                "icmp_source_count": len(sources),
                "_search": search,
                "_refs": refs,
                "_ref_pos": ref_pos,
            }
        )

    labels_path = os.path.join(out_dir, "panel_labels.csv")
    with open(labels_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(panels)

    # ---- result 1: response composition -----------------------------------
    composition = Counter()
    for r, rows in probes_by_round.items():
        for row in rows:
            composition[
                (r, row["selection_arm"], row["root_stratum"], row["response_class"])
            ] += 1
    response_composition = [
        {
            "round": r,
            "selection_arm": arm,
            "root_stratum": stratum,
            "response_class": cls,
            "count": count,
        }
        for (r, arm, stratum, cls), count in sorted(composition.items())
    ]

    # ---- result 2: m=1..5 saturation --------------------------------------
    saturation_overall = []
    previous_cumulative = 0
    for m in range(1, REFERENCE_IIDS + 1):
        cumulative = sum(1 for p in panels if p[f"reference_positive_m{m}"])
        saturation_overall.append(
            {
                "m": m,
                "cumulative": cumulative,
                "cumulative_fraction": cumulative / panel_count,
                "added": cumulative - previous_cumulative,
                "added_fraction": (cumulative - previous_cumulative) / cumulative
                if cumulative
                else 0,
            }
        )
        previous_cumulative = cumulative

    groups = defaultdict(list)
    for p in panels:
        groups[(p["selection_arm"], p["root_stratum"])].append(p)
    saturation_by_group = []
    for (arm, stratum), group in sorted(groups.items()):
        row = {
            "selection_arm": arm,
            "root_stratum": stratum,
            "panel_count": len(group),
        }
        for m in range(1, REFERENCE_IIDS + 1):
            row[f"cumulative_m{m}"] = sum(
                1 for p in group if p[f"reference_positive_m{m}"]
            )
        saturation_by_group.append(row)

    # ---- result 3: search vs reference (m5 label) --------------------------
    search_vs_reference = Counter()
    for p in panels:
        search_vs_reference[
            (
                "search_positive" if p["search_positive"] else "search_no_positive",
                "reference_positive"
                if p["reference_positive_m5"]
                else "reference_no_positive",
            )
        ] += 1
    four_fold = {
        "search_positive_reference_positive": search_vs_reference[
            ("search_positive", "reference_positive")
        ],
        "search_positive_reference_no_positive": search_vs_reference[
            ("search_positive", "reference_no_positive")
        ],
        "search_no_positive_reference_positive": search_vs_reference[
            ("search_no_positive", "reference_positive")
        ],
        "search_no_positive_reference_no_positive": search_vs_reference[
            ("search_no_positive", "reference_no_positive")
        ],
    }
    reference_positive_total = (
        four_fold["search_positive_reference_positive"]
        + four_fold["search_no_positive_reference_positive"]
    )
    four_fold["search_recall"] = (
        four_fold["search_positive_reference_positive"] / reference_positive_total
        if reference_positive_total
        else 0
    )

    # ---- result 4: BGP prior value (paired roots) --------------------------
    root_arms = defaultdict(dict)
    for p in panels:
        root_arms[p["root_prefix"]][p["selection_arm"]] = p
    bgp_four_fold = Counter()
    for arms in root_arms.values():
        if "root_uniform" not in arms or "deepest_bgp_guided" not in arms:
            continue
        uniform_pos = arms["root_uniform"]["reference_positive_m5"]
        guided_pos = arms["deepest_bgp_guided"]["reference_positive_m5"]
        bgp_four_fold[(uniform_pos, guided_pos)] += 1
    bgp = {
        "paired_root_count": sum(bgp_four_fold.values()),
        "uniform_positive_guided_positive": bgp_four_fold[(1, 1)],
        "uniform_positive_guided_no_positive": bgp_four_fold[(1, 0)],
        "uniform_no_positive_guided_positive": bgp_four_fold[(0, 1)],
        "uniform_no_positive_guided_no_positive": bgp_four_fold[(0, 0)],
    }
    uniform_positive = bgp_four_fold[(1, 1)] + bgp_four_fold[(1, 0)]
    guided_positive = bgp_four_fold[(1, 1)] + bgp_four_fold[(0, 1)]
    paired_total = sum(bgp_four_fold.values())
    bgp["uniform_positive_rate"] = uniform_positive / paired_total if paired_total else 0
    bgp["guided_positive_rate"] = guided_positive / paired_total if paired_total else 0

    # ---- result 5: slow-AU threshold sensitivity ---------------------------
    threshold_flips = {}
    for threshold in thresholds:
        flips = 0
        for p in panels:
            baseline = p["reference_positive_m5"]
            at_threshold = any(
                positive_at_threshold(ref, threshold) for ref in p["_refs"]
            )
            if at_threshold != baseline:
                flips += 1
        threshold_flips[str(threshold)] = flips
    threshold_sensitivity = {
        "main_threshold_ms": main_threshold,
        "label_flips_vs_main": threshold_flips,
    }

    # ---- result 6: ICMP source and RTT features ----------------------------
    source_count_distribution = dict(
        Counter(p["icmp_source_count"] for p in panels)
    )
    source_panel_counts = Counter()
    for p in panels:
        sources = {
            row["icmp_source"] for row in [p["_search"]] + p["_refs"] if row["icmp_source"]
        }
        for source in sources:
            source_panel_counts[source] += 1
    source_reuse = {
        "distinct_source_count": len(source_panel_counts),
        "sources_covering_many_panels": [
            {"source": source, "panel_count": count}
            for source, count in source_panel_counts.most_common(20)
        ],
    }

    slow_rtts_positive = [
        float(ref["rtt_ms"])
        for p in panels
        if p["reference_positive_m5"]
        for ref in p["_refs"]
        if ref["response_class"] == "slow_au"
    ]
    slow_rtts_no_positive = [
        float(ref["rtt_ms"])
        for p in panels
        if not p["reference_positive_m5"]
        for ref in p["_refs"]
        if ref["response_class"] == "slow_au"
    ]
    source_and_rtt = {
        "source_count_distribution": source_count_distribution,
        "source_reuse": source_reuse,
        "slow_au_rtt_reference_positive": rtt_stats(slow_rtts_positive),
        "slow_au_rtt_reference_no_positive": rtt_stats(slow_rtts_no_positive),
    }

    summary = {
        "campaign_id": cfg["campaign_id"],
        "output_dir": os.path.abspath(out_dir),
        "validation": {
            "panel_count": panel_count,
            "panels_consistent_across_rounds": panels_consistent,
            "malformed_panel_count": len(malformed_panels),
            "duplicate_probe_id_count": len(duplicate_probe_ids),
            "unexplained_extra_target_count": len(probed_targets - planned_targets),
            "missing_planned_target_count": len(planned_targets - probed_targets),
            "unmatched_by_round": unmatched_by_round,
            "multi_response_by_round": multi_response_by_round,
            "match_status_counts": dict(status_counts),
            "unit_panel_count": len(units),
        },
        "response_composition": response_composition,
        "saturation": {
            "overall": saturation_overall,
            "by_group": saturation_by_group,
        },
        "search_vs_reference_m5": four_fold,
        "bgp_prior_paired": bgp,
        "threshold_sensitivity": threshold_sensitivity,
        "source_and_rtt": source_and_rtt,
    }

    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
