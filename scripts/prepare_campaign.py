#!/usr/bin/env python3
"""Build the BGP prefix prior and its non-overlapping top-level roots.

It preserves every valid BGP prefix as evidence, uses only top-level prefixes
for non-overlapping frame accounting, and reports the response-blind root
features needed to define calibration/held-out strata.  With explicit split
options, it assigns whole roots within fixed depth strata; it never splits a
top-level search root.  It can also emit deterministic calibration C64 panels
and disjoint IID rounds after the split has been fixed.
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


def load_exclusions(path):
    exclusions = []
    if not path:
        return exclusions
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            try:
                prefix = ipaddress.ip_network(text, strict=True)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid exclusion {text!r}"
                ) from exc
            if prefix.version != 6:
                raise ValueError(f"{path}:{line_number}: non-IPv6 exclusion {prefix}")
            exclusions.append(prefix)
    return exclusions


def load_rows(path, exclusions):
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
            containing_exclusions = [
                item for item in exclusions if prefix.subnet_of(item)
            ]
            if containing_exclusions:
                excluded[f"configured:{containing_exclusions[0]}"] += 1
                continue
            partial_exclusions = [
                item for item in exclusions if item.subnet_of(prefix)
            ]
            if partial_exclusions:
                raise ValueError(
                    f"input prefix {prefix} contains configured exclusion "
                    f"{partial_exclusions[0]}; partial-prefix subtraction is unsupported"
                )
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


def hash_int(*parts):
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def select_c64(prefix, seed, *parts):
    c64_count = 1 << (64 - prefix.prefixlen)
    offset = hash_int(seed, *parts) % c64_count
    address = int(prefix.network_address) + (offset << 64)
    return ipaddress.ip_network((address, 64))


def select_guided_c64(root, candidates, uniform_c64, seed):
    ranked = sorted(
        candidates,
        key=lambda prefix: (
            hash_int(seed, root, "guided-prefix", prefix),
            int(prefix.network_address),
            prefix.prefixlen,
        ),
    )
    for prefix in ranked:
        candidate = select_c64(prefix, seed, root, "guided-c64", prefix)
        if candidate != uniform_c64:
            return candidate, prefix
        c64_count = 1 << (64 - prefix.prefixlen)
        if c64_count > 1:
            next_address = int(candidate.network_address) + (1 << 64)
            if next_address >= int(prefix.broadcast_address) + 1:
                next_address = int(prefix.network_address)
            return ipaddress.ip_network((next_address, 64)), prefix
    return None, None


def panel_id(seed, root, arm):
    digest = hashlib.sha256(f"{seed}\0{root}\0{arm}".encode("ascii")).hexdigest()
    return digest[:20]


def panel_targets(seed, panel, reference_iids):
    used_iids = set()
    rows = []
    roles = [("search", 0)] + [
        ("reference", index) for index in range(1, reference_iids + 1)
    ]
    for role, index in roles:
        counter = 0
        while True:
            iid = hash_int(seed, panel["c64"], role, index, counter) >> 192
            if iid != 0 and iid not in used_iids:
                break
            counter += 1
        used_iids.add(iid)
        c64 = ipaddress.ip_network(panel["c64"])
        target = ipaddress.ip_address(int(c64.network_address) | iid)
        round_name = "search" if role == "search" else f"reference-{index}"
        rows.append(
            {
                "probe_id": f"{panel['panel_id']}:{role}:{index}",
                "panel_id": panel["panel_id"],
                "tranche": "calibration",
                "root_stratum": panel["root_stratum"],
                "root_prefix": panel["root_prefix"],
                "selection_arm": panel["selection_arm"],
                "c64": panel["c64"],
                "target_ipv6": str(target),
                "role": role,
                "iid_index": index,
                "round": round_name,
            }
        )
    return rows


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


def write_lines(path, lines):
    if os.path.exists(path):
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")


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
    parser.add_argument("--exclude-prefix-file")
    parser.add_argument("--split-seed")
    parser.add_argument("--calibration-root-fraction", type=parse_fraction)
    parser.add_argument("--calibration-target-seed")
    parser.add_argument("--reference-iids", type=int, default=5)
    args = parser.parse_args(argv)

    if bool(args.split_seed) != bool(args.calibration_root_fraction):
        parser.error(
            "--split-seed and --calibration-root-fraction must be supplied together"
        )
    if args.calibration_target_seed and not args.split_seed:
        parser.error("calibration targets require the root split options")
    if not 1 <= args.reference_iids <= 32:
        parser.error("--reference-iids must be between 1 and 32")

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    try:
        exclusions = load_exclusions(args.exclude_prefix_file)
        metadata, excluded = load_rows(args.input, exclusions)
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

        deepest_prefixes_by_root = defaultdict(list)
        for prefix in prefixes:
            root = root_for(prefix, parents, root_cache)
            if prefix != root and depth_cache[prefix] == root_max_tree_depth[root]:
                deepest_prefixes_by_root[root].append(prefix)

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

        calibration_units = []
        calibration_targets = []
        guided_skipped = 0
        if args.calibration_target_seed:
            for root in sorted(roots, key=lambda p: (int(p.network_address), p.prefixlen)):
                if tranche_by_root[root] != "calibration":
                    continue
                stratum = root_depth_stratum(root_max_tree_depth[root])
                root_calibration_pi = Fraction(
                    split_stratum_counts[stratum]["calibration_root_count"],
                    split_stratum_counts[stratum]["root_count"],
                )
                root_c64_count = 1 << (64 - root.prefixlen)
                uniform_c64 = select_c64(
                    root, args.calibration_target_seed, root, "root-uniform"
                )
                panels = [
                    {
                        "panel_id": panel_id(
                            args.calibration_target_seed, root, "root-uniform"
                        ),
                        "root_stratum": stratum,
                        "root_prefix": str(root),
                        "selection_arm": "root_uniform",
                        "selection_prefix": str(root),
                        "selection_prefix_length": root.prefixlen,
                        "selection_bgp_tree_depth": 0,
                        "deepest_candidate_count": len(
                            deepest_prefixes_by_root[root]
                        ),
                        "c64": str(uniform_c64),
                        "root_calibration_pi": str(root_calibration_pi),
                        "conditional_c64_pi": str(Fraction(1, root_c64_count)),
                        "c64_inclusion_pi": str(
                            root_calibration_pi / root_c64_count
                        ),
                    }
                ]
                if root_max_tree_depth[root] > 0:
                    guided_c64, guided_prefix = select_guided_c64(
                        root,
                        deepest_prefixes_by_root[root],
                        uniform_c64,
                        args.calibration_target_seed,
                    )
                    if guided_c64 is None:
                        guided_skipped += 1
                    else:
                        panels.append(
                            {
                                "panel_id": panel_id(
                                    args.calibration_target_seed,
                                    root,
                                    "deepest-bgp-guided",
                                ),
                                "root_stratum": stratum,
                                "root_prefix": str(root),
                                "selection_arm": "deepest_bgp_guided",
                                "selection_prefix": str(guided_prefix),
                                "selection_prefix_length": guided_prefix.prefixlen,
                                "selection_bgp_tree_depth": depth_cache[guided_prefix],
                                "deepest_candidate_count": len(
                                    deepest_prefixes_by_root[root]
                                ),
                                "c64": str(guided_c64),
                                "root_calibration_pi": str(root_calibration_pi),
                                "conditional_c64_pi": "",
                                "c64_inclusion_pi": "",
                            }
                        )
                for panel in panels:
                    calibration_units.append(panel)
                    calibration_targets.extend(
                        panel_targets(
                            args.calibration_target_seed,
                            panel,
                            args.reference_iids,
                        )
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

        target_output_paths = {}
        if args.calibration_target_seed:
            units_path = os.path.join(args.output_dir, "calibration_units.csv")
            targets_path = os.path.join(args.output_dir, "calibration_targets.csv")
            write_csv(units_path, list(calibration_units[0]), calibration_units)
            write_csv(targets_path, list(calibration_targets[0]), calibration_targets)
            target_output_paths["calibration_units"] = os.path.abspath(units_path)
            target_output_paths["calibration_targets"] = os.path.abspath(targets_path)
            for round_name in ["search"] + [
                f"reference-{index}" for index in range(1, args.reference_iids + 1)
            ]:
                rows = [
                    row for row in calibration_targets if row["round"] == round_name
                ]
                rows.sort(
                    key=lambda row: hash_int(
                        args.calibration_target_seed,
                        "round-order",
                        round_name,
                        row["target_ipv6"],
                    )
                )
                path = os.path.join(args.output_dir, f"targets-{round_name}.txt")
                write_lines(path, [row["target_ipv6"] for row in rows])
                target_output_paths[f"targets_{round_name}"] = os.path.abspath(path)

        summary = {
            "input": os.path.abspath(args.input),
            "exclude_prefix_file": (
                os.path.abspath(args.exclude_prefix_file)
                if args.exclude_prefix_file
                else None
            ),
            "configured_exclusions": [str(prefix) for prefix in exclusions],
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
        if args.calibration_target_seed:
            arm_count = Counter(
                panel["selection_arm"] for panel in calibration_units
            )
            round_count = Counter(row["round"] for row in calibration_targets)
            summary["calibration_targets"] = {
                "target_seed": args.calibration_target_seed,
                "reference_iids_per_c64": args.reference_iids,
                "panel_count": len(calibration_units),
                "probe_count": len(calibration_targets),
                "selection_arm_counts": dict(arm_count),
                "round_counts": dict(round_count),
                "guided_roots_skipped_due_to_collision": guided_skipped,
                "outputs": target_output_paths,
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
