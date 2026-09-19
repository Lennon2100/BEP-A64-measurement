#!/usr/bin/env python3
"""Build the BGP prefix prior and its non-overlapping top-level roots.

This first preparation slice deliberately stops before C64 sampling and IID
generation.  Those require campaign budgets and strata that are not yet
chosen.  It preserves every valid BGP prefix as evidence, uses only top-level
prefixes for non-overlapping frame accounting, and reports the response-blind
root features needed to define calibration/held-out strata.  With explicit
split options, it assigns whole roots within fixed depth strata; it never
splits a top-level search root.
"""

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict
from fractions import Fraction

from count_prefix_nesting import build_immediate_parent_map, project_dir


def parse_fraction(text):
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("fraction must be a decimal or ratio") from exc
    if not 0 < value < 1:
        raise argparse.ArgumentTypeError("fraction must be between 0 and 1")
    return value


def load_rows(path):
    rows = {}
    excluded = Counter()
    with open(path, newline="", encoding="utf-8") as fh:
        for line_number, fields in enumerate(csv.reader(fh), 1):
            if not fields or not any(field.strip() for field in fields):
                continue
            if fields[0].lstrip().startswith("#"):
                continue
            if fields[0].strip().lower() == "prefix":
                continue
            if len(fields) != 3:
                raise ValueError(
                    f"{path}:{line_number}: expected 3 CSV fields, got {len(fields)}"
                )

            prefix_text, count_text, origins_text = (field.strip() for field in fields)
            try:
                prefix = ipaddress.ip_network(prefix_text, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid prefix {prefix_text!r}"
                ) from exc
            if prefix.version != 6:
                raise ValueError(f"{path}:{line_number}: non-IPv6 prefix {prefix}")

            try:
                declared_count = int(count_text)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid origin count {count_text!r}"
                ) from exc
            origins = tuple(dict.fromkeys(x for x in origins_text.split("|") if x))
            if declared_count != len(origins):
                raise ValueError(
                    f"{path}:{line_number}: origin count {declared_count} "
                    f"does not match {len(origins)} origin values"
                )
            if prefix in rows:
                raise ValueError(f"{path}:{line_number}: duplicate prefix {prefix}")

            if prefix.prefixlen == 0:
                excluded["default_route"] += 1
                continue
            if prefix.prefixlen > 64:
                excluded["longer_than_64"] += 1
                continue
            rows[prefix] = {
                "origin_count": declared_count,
                "origins": origins,
            }
    if not rows:
        raise ValueError("input contains no usable IPv6 prefixes at /64 or shorter")
    return rows, excluded


def root_for(prefix, parents, cache):
    path = []
    current = prefix
    while current in parents:
        path.append(current)
        current = parents[current]
    for item in path:
        cache[item] = current
    cache[prefix] = current
    return current


def tree_depth_for(prefix, parents, cache):
    if prefix in cache:
        return cache[prefix]
    path = []
    current = prefix
    while current in parents and current not in cache:
        path.append(current)
        current = parents[current]
    depth = cache.get(current, 0)
    for item in reversed(path):
        depth += 1
        cache[item] = depth
    cache.setdefault(prefix, depth)
    return cache[prefix]


def root_depth_stratum(max_tree_depth):
    if max_tree_depth >= 3:
        return "d3plus"
    return f"d{max_tree_depth}"


def assign_root_tranches(roots_by_stratum, seed, calibration_fraction):
    assignments = {}
    counts = {}
    for stratum in sorted(roots_by_stratum):
        roots = roots_by_stratum[stratum]
        ranked = sorted(
            roots,
            key=lambda root: hashlib.sha256(
                f"{seed}\0{stratum}\0{root}".encode("ascii")
            ).digest()
            + int(root.network_address).to_bytes(16, "big"),
        )
        calibration_count = (
            len(ranked) * calibration_fraction.numerator
            + calibration_fraction.denominator // 2
        ) // calibration_fraction.denominator
        if len(ranked) >= 2:
            calibration_count = min(max(calibration_count, 1), len(ranked) - 1)
        elif ranked:
            raise ValueError(
                f"root stratum {stratum} has one root and cannot be split"
            )
        calibration = set(ranked[:calibration_count])
        for root in ranked:
            assignments[root] = (
                "calibration" if root in calibration else "held_out"
            )
        counts[stratum] = {
            "root_count": len(ranked),
            "calibration_root_count": calibration_count,
            "held_out_root_count": len(ranked) - calibration_count,
        }
    return assignments, counts


def write_csv(path, fieldnames, rows):
    if os.path.exists(path):
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default=os.path.join(
            project_dir(), "data", "interim", "ris_ipv6_prefixes_unique.csv"
        ),
    )
    parser.add_argument("output_dir")
    parser.add_argument("--split-seed")
    parser.add_argument("--calibration-root-fraction", type=parse_fraction)
    args = parser.parse_args(argv)

    if bool(args.split_seed) != bool(args.calibration_root_fraction):
        parser.error(
            "--split-seed and --calibration-root-fraction must be supplied together"
        )

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    try:
        metadata, excluded = load_rows(args.input)
        prefixes = set(metadata)
        parents = build_immediate_parent_map(prefixes)
        children = defaultdict(list)
        for child, parent in parents.items():
            children[parent].append(child)

        roots = prefixes - set(parents)
        root_cache = {}
        depth_cache = {root: 0 for root in roots}
        descendant_count = Counter()
        for prefix in sorted(prefixes, key=lambda p: p.prefixlen, reverse=True):
            if prefix in parents:
                descendant_count[parents[prefix]] += 1 + descendant_count[prefix]
        root_max_tree_depth = Counter()
        tree_rows = []
        for prefix in sorted(prefixes, key=lambda p: (int(p.network_address), p.prefixlen)):
            root = root_for(prefix, parents, root_cache)
            tree_depth = tree_depth_for(prefix, parents, depth_cache)
            root_max_tree_depth[root] = max(root_max_tree_depth[root], tree_depth)
            tree_rows.append(
                {
                    "prefix": str(prefix),
                    "prefix_length": prefix.prefixlen,
                    "origin_count": metadata[prefix]["origin_count"],
                    "origins": "|".join(metadata[prefix]["origins"]),
                    "parent_prefix": str(parents[prefix]) if prefix in parents else "",
                    "root_prefix": str(root),
                    "bit_depth_from_root": prefix.prefixlen - root.prefixlen,
                    "bgp_tree_depth": tree_depth,
                    "direct_child_count": len(children[prefix]),
                    "descendant_prefix_count": descendant_count[prefix],
                    "is_top_level": int(prefix == root),
                }
            )

        roots_by_stratum = defaultdict(list)
        for root in roots:
            roots_by_stratum[root_depth_stratum(root_max_tree_depth[root])].append(root)
        tranche_by_root = {}
        split_stratum_counts = {}
        if args.split_seed:
            tranche_by_root, split_stratum_counts = assign_root_tranches(
                roots_by_stratum, args.split_seed, args.calibration_root_fraction
            )

        for row in tree_rows:
            root = ipaddress.ip_network(row["root_prefix"])
            row["root_stratum"] = root_depth_stratum(root_max_tree_depth[root])
            row["tranche"] = tranche_by_root.get(root, "")

        root_rows = []
        total_c64_count = 0
        root_length_count = Counter()
        root_length_c64_count = Counter()
        root_tree_depth_count = Counter()
        tranche_root_count = Counter()
        tranche_c64_count = Counter()
        stratum_tranche_c64_count = defaultdict(Counter)
        for root in sorted(roots, key=lambda p: (int(p.network_address), p.prefixlen)):
            c64_count = 1 << (64 - root.prefixlen)
            stratum = root_depth_stratum(root_max_tree_depth[root])
            tranche = tranche_by_root.get(root, "")
            total_c64_count += c64_count
            root_length_count[root.prefixlen] += 1
            root_length_c64_count[root.prefixlen] += c64_count
            root_tree_depth_count[root_max_tree_depth[root]] += 1
            if tranche:
                tranche_root_count[tranche] += 1
                tranche_c64_count[tranche] += c64_count
                stratum_tranche_c64_count[stratum][tranche] += c64_count
            root_rows.append(
                {
                    "root_prefix": str(root),
                    "prefix_length": root.prefixlen,
                    "origin_count": metadata[root]["origin_count"],
                    "origins": "|".join(metadata[root]["origins"]),
                    "direct_child_count": len(children[root]),
                    "descendant_prefix_count": descendant_count[root],
                    "max_bgp_tree_depth": root_max_tree_depth[root],
                    "routed_c64_count": c64_count,
                    "root_stratum": stratum,
                    "tranche": tranche,
                }
            )

        os.makedirs(args.output_dir, exist_ok=True)
        tree_path = os.path.join(args.output_dir, "bgp_tree.csv")
        roots_path = os.path.join(args.output_dir, "frame_roots.csv")
        summary_path = os.path.join(args.output_dir, "summary.json")
        if os.path.exists(summary_path):
            raise FileExistsError(
                f"refusing to overwrite existing output: {summary_path}"
            )

        write_csv(tree_path, list(tree_rows[0]), tree_rows)
        write_csv(roots_path, list(root_rows[0]), root_rows)

        summary = {
            "input": os.path.abspath(args.input),
            "usable_prefix_count": len(prefixes),
            "nested_prefix_count": len(parents),
            "top_level_prefix_count": len(roots),
            "parent_prefix_count": sum(bool(children[p]) for p in prefixes),
            "excluded": dict(excluded),
            "routed_c64_count": total_c64_count,
            "root_prefix_length_histogram": {
                str(length): root_length_count[length]
                for length in sorted(root_length_count)
            },
            "routed_c64_by_root_prefix_length": {
                str(length): root_length_c64_count[length]
                for length in sorted(root_length_c64_count)
            },
            "root_max_bgp_tree_depth_histogram": {
                str(depth): root_tree_depth_count[depth]
                for depth in sorted(root_tree_depth_count)
            },
            "outputs": {
                "bgp_tree": os.path.abspath(tree_path),
                "frame_roots": os.path.abspath(roots_path),
            },
        }
        if args.split_seed:
            for stratum in split_stratum_counts:
                split_stratum_counts[stratum]["calibration_routed_c64_count"] = (
                    stratum_tranche_c64_count[stratum]["calibration"]
                )
                split_stratum_counts[stratum]["held_out_routed_c64_count"] = (
                    stratum_tranche_c64_count[stratum]["held_out"]
                )
            summary["root_split"] = {
                "method": "exact_hash_rank_within_root_depth_stratum",
                "seed": args.split_seed,
                "calibration_root_fraction": str(args.calibration_root_fraction),
                "strata": split_stratum_counts,
                "tranches": {
                    tranche: {
                        "root_count": tranche_root_count[tranche],
                        "routed_c64_count": tranche_c64_count[tranche],
                    }
                    for tranche in ("calibration", "held_out")
                },
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
