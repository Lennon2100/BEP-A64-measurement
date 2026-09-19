#!/usr/bin/env python3
"""Build the BGP prefix prior and freeze a block-level campaign split.

This first preparation slice deliberately stops before C64 sampling and IID
generation.  Those require campaign budgets and strata that are not yet
chosen.  It preserves every valid BGP prefix as evidence, uses only top-level
prefixes for non-overlapping frame accounting.  Roots shorter than the recorded
split length are deterministically subdivided before assignment, so one large
aggregate cannot dominate calibration or held-out.  The default split length
is /32, which is also the planned search start level.
"""

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from count_prefix_nesting import build_immediate_parent_map, project_dir


def parse_fraction(text):
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("fraction must be a decimal") from exc
    if not Decimal(0) < value < Decimal(1):
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


def split_for(block, seed, calibration_fraction):
    digest = hashlib.sha256(f"{seed}\0{block}".encode("ascii")).digest()
    value = int.from_bytes(digest, "big")
    threshold = int(calibration_fraction * (1 << 256))
    return "calibration" if value < threshold else "held_out"


def frame_blocks(roots, split_prefix_length):
    for root in sorted(roots, key=lambda p: (int(p.network_address), p.prefixlen)):
        if root.prefixlen < split_prefix_length:
            for block in root.subnets(new_prefix=split_prefix_length):
                yield block, root
        else:
            yield root, root


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
    parser.add_argument("--seed", required=True)
    parser.add_argument(
        "--calibration-block-fraction", required=True, type=parse_fraction
    )
    parser.add_argument("--split-prefix-length", type=int, default=32)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    try:
        metadata, excluded = load_rows(args.input)
        if not 1 <= args.split_prefix_length <= 64:
            raise ValueError("split prefix length must be between 1 and 64")
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
        blocks = list(frame_blocks(roots, args.split_prefix_length))
        split_by_block = {
            block: split_for(block, args.seed, args.calibration_block_fraction)
            for block, _root in blocks
        }
        if len(set(split_by_block.values())) != 2:
            raise ValueError("block split produced an empty calibration or held-out part")

        tree_rows = []
        for prefix in sorted(prefixes, key=lambda p: (int(p.network_address), p.prefixlen)):
            root = root_for(prefix, parents, root_cache)
            if prefix.prefixlen < args.split_prefix_length and root.prefixlen < args.split_prefix_length:
                tranche = "mixed"
                split_block = ""
            else:
                split_block_net = (
                    root
                    if root.prefixlen >= args.split_prefix_length
                    else prefix.supernet(new_prefix=args.split_prefix_length)
                )
                tranche = split_by_block[split_block_net]
                split_block = str(split_block_net)
            tree_rows.append(
                {
                    "prefix": str(prefix),
                    "prefix_length": prefix.prefixlen,
                    "origin_count": metadata[prefix]["origin_count"],
                    "origins": "|".join(metadata[prefix]["origins"]),
                    "parent_prefix": str(parents[prefix]) if prefix in parents else "",
                    "root_prefix": str(root),
                    "bit_depth_from_root": prefix.prefixlen - root.prefixlen,
                    "bgp_tree_depth": tree_depth_for(prefix, parents, depth_cache),
                    "direct_child_count": len(children[prefix]),
                    "descendant_prefix_count": descendant_count[prefix],
                    "is_top_level": int(prefix == root),
                    "split_block": split_block,
                    "tranche": tranche,
                }
            )

        block_rows = []
        split_block_count = Counter()
        split_c64_count = Counter()
        for block, root in blocks:
            tranche = split_by_block[block]
            c64_count = 1 << (64 - block.prefixlen)
            split_block_count[tranche] += 1
            split_c64_count[tranche] += c64_count
            block_rows.append(
                {
                    "split_block": str(block),
                    "split_prefix_length": block.prefixlen,
                    "root_prefix": str(root),
                    "routed_c64_count": c64_count,
                    "tranche": tranche,
                }
            )

        os.makedirs(args.output_dir, exist_ok=True)
        tree_path = os.path.join(args.output_dir, "bgp_tree.csv")
        blocks_path = os.path.join(args.output_dir, "frame_blocks.csv")
        summary_path = os.path.join(args.output_dir, "summary.json")
        if os.path.exists(summary_path):
            raise FileExistsError(
                f"refusing to overwrite existing output: {summary_path}"
            )

        write_csv(tree_path, list(tree_rows[0]), tree_rows)
        write_csv(blocks_path, list(block_rows[0]), block_rows)

        summary = {
            "input": os.path.abspath(args.input),
            "seed": args.seed,
            "calibration_block_fraction": str(args.calibration_block_fraction),
            "split_prefix_length": args.split_prefix_length,
            "usable_prefix_count": len(prefixes),
            "nested_prefix_count": len(parents),
            "top_level_prefix_count": len(roots),
            "parent_prefix_count": sum(bool(children[p]) for p in prefixes),
            "excluded": dict(excluded),
            "routed_c64_count": sum(split_c64_count.values()),
            "tranches": {
                name: {
                    "block_count": split_block_count[name],
                    "routed_c64_count": split_c64_count[name],
                }
                for name in ("calibration", "held_out")
            },
            "outputs": {
                "bgp_tree": os.path.abspath(tree_path),
                "frame_blocks": os.path.abspath(blocks_path),
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
