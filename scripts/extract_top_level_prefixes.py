#!/usr/bin/env python3
"""Extract the top-level (non-overlapping) prefixes for SubRecon's probe start.

Reads the three-field RIS prefix file (`prefix,origin_count,origins`), drops
every prefix that is contained inside a shorter prefix, and writes only the
top-level prefixes to a new file. The input file is opened read-only and is
never modified.
"""

import argparse
import csv
import ipaddress
import os
import sys

from count_prefix_nesting import build_immediate_parent_map, project_dir


def load_rows(path):
    """Return {prefix: (origin_count, origins)} for usable frame prefixes."""
    rows = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for line_number, fields in enumerate(csv.reader(fh), 1):
            if not fields or not any(f.strip() for f in fields):
                continue
            if fields[0].lstrip().startswith("#"):
                continue
            if fields[0].strip().lower() == "prefix":
                continue
            if len(fields) != 3:
                raise ValueError(
                    f"{path}:{line_number}: expected 3 fields, got {len(fields)}"
                )
            prefix_text, count_text, origins_text = (f.strip() for f in fields)
            try:
                prefix = ipaddress.ip_network(prefix_text, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid prefix {prefix_text!r}"
                ) from exc
            if prefix.version != 6:
                raise ValueError(f"{path}:{line_number}: non-IPv6 prefix {prefix}")
            if prefix.prefixlen == 0:
                continue  # ::/0 is a catch-all, not a routed block
            if prefix.prefixlen > 64:
                continue  # longer than /64 does not define a whole /64
            if prefix in rows:
                raise ValueError(f"{path}:{line_number}: duplicate prefix {prefix}")
            rows[prefix] = (count_text, origins_text)
    return rows


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default=os.path.join(
            project_dir(), "data", "interim", "ris_ipv6_prefixes_unique.csv"
        ),
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=os.path.join(
            project_dir(), "data", "interim", "ris_ipv6_top_level_prefixes.csv"
        ),
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    rows = load_rows(args.input)
    prefixes = set(rows)
    parents = build_immediate_parent_map(prefixes)
    top_level = sorted(
        prefixes - set(parents),
        key=lambda p: (int(p.network_address), p.prefixlen),
    )

    if os.path.exists(args.output):
        print(f"refusing to overwrite existing output: {args.output}", file=sys.stderr)
        return 1

    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["prefix", "origin_count", "origins"])
        for prefix in top_level:
            count_text, origins_text = rows[prefix]
            writer.writerow([str(prefix), count_text, origins_text])

    print(f"input file: {args.input}")
    print(f"usable frame prefixes kept (IPv6, /1../64): {len(rows)}")
    print(f"stripped nested prefixes: {len(parents)}")
    print(f"top-level prefixes written: {len(top_level)}")
    print(f"output file: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
