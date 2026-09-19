#!/usr/bin/env python3
"""Report parent-child (nested) prefix relationships in a deduplicated RIS CSV.

Reads the output of dedup_ris_prefixes.sh (one unique IPv6 prefix per row, the
prefix in the first comma-separated field) and reports the counts needed to
build the full-C64 frame under D042/D044:

  - total unique prefixes
  - the IPv6 default route ::/0 (excluded: a catch-all, not a real routed block)
  - prefixes longer than /64 (dropped from the frame: they do not define a whole C64)
  - prefixes strictly contained inside a shorter prefix (redundant for address space)
  - top-level prefixes (no covering shorter prefix) that remain for the frame
  - the exact union size N = sum of 2**(64 - prefixlen) over the top-level prefixes

Usage:
    count_prefix_nesting.py [input.csv]

The default input is data/interim/ris_ipv6_prefixes_unique.csv under the
project root (the directory above scripts/). The path is anchored to this
script's own location, so it works regardless of where the project is checked
out or under which name it is deployed.
"""
import argparse
import ipaddress
import os
import sys
from collections import Counter

COARSE_LIMIT = 27  # list actual CIDRs for top-level prefixes at or coarser than this


def project_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_prefixes(path):
    prefixes = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            text = line.split(",")[0].strip()
            if not text:
                continue
            try:
                prefixes.add(ipaddress.ip_network(text, strict=False))
            except ValueError:
                print(f"skipping unparsable prefix: {text!r}", file=sys.stderr)
    return prefixes


def build_immediate_parent_map(prefixes):
    """Return child -> nearest covering prefix for a canonical prefix set."""
    prefix_set = set(prefixes)
    edges = {}
    for prefix in prefix_set:
        ancestor = prefix
        while ancestor.prefixlen > 0:
            ancestor = ancestor.supernet()
            if ancestor in prefix_set:
                edges[prefix] = ancestor
                break
    return edges


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default=os.path.join(project_dir(), "data", "interim",
                             "ris_ipv6_prefixes_unique.csv"),
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    all_prefixes = load_prefixes(args.input)

    # D042/D044: announcements longer than /64 do not define a whole C64. The IPv6
    # default route ::/0 (prefixlen 0) is a catch-all, not a routed block, so
    # it is excluded from the frame as well.
    long_prefixes = {p for p in all_prefixes if p.prefixlen > 64}
    default_routes = {p for p in all_prefixes if p.prefixlen == 0}
    frame_prefixes = all_prefixes - long_prefixes - default_routes

    # A prefix is nested if any strictly-shorter prefix in the set contains it.
    # Walk ancestors one level at a time (at most 64 steps) to keep this O(n),
    # and record each nested prefix's immediate covering prefix (parent).
    edges = build_immediate_parent_map(frame_prefixes)

    nested = set(edges)
    top_level = frame_prefixes - nested
    total_c64 = sum(2 ** (64 - p.prefixlen) for p in top_level)

    print(f"input file: {args.input}")
    print(f"total unique prefixes: {len(all_prefixes)}")
    print(f"default route ::/0 (excluded from frame): {len(default_routes)}")
    print(f"prefixes longer than /64 (dropped from frame): {len(long_prefixes)}")
    print(f"prefixes nested inside a shorter prefix (redundant): {len(nested)}")
    print(f"top-level prefixes (frame): {len(top_level)}")
    print(f"prefixes acting as an immediate parent: {len(set(edges.values()))}")
    print(f"union C64 count N = {total_c64}  (~{total_c64:.3e})")

    hist = Counter(p.prefixlen for p in top_level)
    print("top-level prefix-length histogram (prefixlen: count):")
    for length in sorted(hist):
        print(f"  /{length:<3} {hist[length]}")

    coarse = sorted(
        (p for p in top_level if p.prefixlen <= COARSE_LIMIT),
        key=lambda p: (p.prefixlen, p.network_address),
    )
    print(f"coarse top-level prefixes (prefixlen <= /{COARSE_LIMIT}):")
    for p in coarse:
        print(f"  {p}")

    long_sorted = sorted(long_prefixes,
                         key=lambda p: (p.prefixlen, p.network_address))
    print(f"\n=== prefixes longer than /64 (dropped, {len(long_sorted)}) ===")
    for p in long_sorted:
        print(f"  {p}")

    edge_sorted = sorted(
        edges.items(),
        key=lambda kv: (kv[1].prefixlen, kv[1].network_address,
                        kv[0].prefixlen, kv[0].network_address),
    )
    print(f"\n=== parent-child relationships (child,parent; {len(edge_sorted)} edges) ===")
    for child, parent in edge_sorted:
        print(f"  {child},{parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
