#!/usr/bin/env python3
"""Generate the bounded calibration action-trace targets (D054-D056).

The D050 panel observes only one or two C64s per root, so it cannot rank the
dynamic expansion lengths `d in {1,4,8}` against plain sampling.  This producer
emits the single formal ordered target set that answers that question inside
calibration roots only; held-out roots are never touched.  There is no separate
one-tenth pilot (D056): the completed D050 observations are the common
pre-action context, this trace is itself formal Internet calibration
measurement, and its predeclared total probe budget bounds the largest observed
horizon.

Actions (equal probe cost, see D054):

  sample      probe C64s uniformly at random across the whole root;
  split-d     subdivide the root into 2**d equal children, rank the children by
              the preprobe BGP more-specific prior, and concentrate probes in
              the top-ranked children.

`split-d` assigns probes round-robin over the children in prior-rank order, so
the first `min(k, 2**d)` probes land one each in the top-ranked children and
later probes revisit those children in the same order (D055 permits multiple
distinct C64s inside one selected child when 2**d < k).  A child's prior score
is (number of BGP prefixes at or below that child's granularity, deepest such
prefix below the child), so a child that contains deeper/more BGP more-specifics
ranks higher; ties break on a deterministic hash.  This is the same
deepest-more-specific prior measured in D052/D053, restricted to the child grid
of the chosen `d`.

Each selected root runs every valid action on mutually disjoint C64 sets so the
within-root comparison is paired and no C64 is probed twice.  `--horizon` is
only the maximum ordered window (D055/D056); horizons {1,2,4,8,16,32} are
evaluated by taking prefixes of each action's ordered sequence, so the analysis
never depends on a fixed per-node batch.  A root with fewer than 4*horizon C64s
is truncated to `N_v // 4` probes per action, and a split action is omitted when
`len(root)+d > 64`.  No universal batch size and no mandatory per-node minimum
is claimed (D056); the achieved `k` and its detection sensitivity are reported
by the analyzer, and `defer` remains out of scope (D054).

Outputs (all deterministic for the same inputs and seeds):

  action_trace_targets.csv   one row per probe with its action metadata;
  action_trace_targets.txt   the same targets in a globally hash-shuffled order;
  action_trace_summary.json  run provenance, predeclared budget, per-stratum counts.
"""

import argparse
import csv
import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict

import prepare_campaign as pc

SPLIT_DEPTHS = (1, 4, 8)
HORIZONS = (1, 2, 4, 8, 16, 32)

TRACE_FIELDS = [
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
]


def action_c64(prefix, seed, action_id, d, order, counter):
    """One deterministic C64 inside `prefix`, keyed by the probe identity."""
    return pc.select_c64(prefix, seed, "action-trace", action_id, d, order, counter)


def next_unused_c64(prefix, seed, action_id, d, order, used):
    """Deterministic C64 in `prefix` not already used by any action of this root.

    Returns None when no unused C64 remains in `prefix`.  Small prefixes are
    enumerated in a shuffled order so the result is exact even when a split
    child is a single C64 that another action already took; large prefixes use a
    bounded hash scan because `used` stays tiny relative to their size.
    """
    c64_count = 1 << (64 - prefix.prefixlen)
    base = int(prefix.network_address)
    if c64_count <= 4096:
        offsets = list(range(c64_count))
        offsets.sort(
            key=lambda offset: pc.hash_int(
                seed, "c64-shuffle", action_id, d, order, offset
            )
        )
        for offset in offsets:
            c64 = ipaddress.ip_network((base + (offset << 64), 64))
            if c64 not in used:
                used.add(c64)
                return c64
        return None
    for counter in range(min(c64_count, 4096)):
        c64 = action_c64(prefix, seed, action_id, d, order, counter)
        if c64 not in used:
            used.add(c64)
            return c64
    return None


def probe_iid(c64, seed):
    """One non-zero search IID for a C64 (each C64 receives exactly one probe)."""
    counter = 0
    while True:
        iid = pc.hash_int(seed, "action-trace-iid", c64, counter) >> 192
        if iid != 0:
            return iid
        counter += 1


def child_prior(children, descendants, d, seed):
    """Rank the 2**d children by the preprobe BGP more-specific prior.

    `descendants` are the BGP prefixes strictly inside the root.  A child scores
    one point per BGP prefix at or below its own granularity and records the
    deepest such prefix below the child; coarser-than-child announcements span
    several children and contribute no differential signal at this `d`.
    """
    child_len = children[0].prefixlen
    count = Counter()
    maxdepth = Counter()
    for prefix in descendants:
        if prefix.prefixlen < child_len:
            continue
        child = prefix.supernet(new_prefix=child_len)
        count[child] += 1
        maxdepth[child] = max(maxdepth[child], prefix.prefixlen - child.prefixlen)
    return sorted(
        children,
        key=lambda child: (
            -count[child],
            -maxdepth[child],
            pc.hash_int(seed, "child-tiebreak", d, child),
        ),
    )


def sample_sequence(root, k, seed, used):
    for order in range(k):
        c64 = next_unused_c64(root, seed, "sample", 0, order, used)
        if c64 is None:
            break
        yield order, "", c64


def split_sequence(root, d, k, seed, used, children_ranked):
    count = len(children_ranked)
    emitted = 0
    child_index = 0
    misses = 0
    while emitted < k and misses < count:
        child = children_ranked[child_index % count]
        c64 = next_unused_c64(child, seed, "split", d, emitted, used)
        if c64 is None:
            child_index += 1
            misses += 1
            continue
        yield emitted, str(child), c64
        emitted += 1
        child_index += 1
        misses = 0


def make_probe(action_id, d, root, stratum, order, child, c64, iid):
    target = str(ipaddress.ip_address(int(c64.network_address) | iid))
    return {
        "probe_id": f"{root}@{action_id}@{order}",
        "action_id": action_id,
        "parent_prefix": str(root),
        "prefix_length": str(root.prefixlen),
        "c64_capacity": str(1 << (64 - root.prefixlen)),
        "d": str(d),
        "probe_order": str(order),
        "selected_child": child,
        "target_c64": str(c64),
        "target_ipv6": target,
        "iid": str(iid),
        "root_stratum": stratum,
        "probe_cost": "1",
    }


def generate_root_probes(root, stratum, descendants, seed, horizon):
    """Run every valid action on disjoint C64 sets; return (probes, per_action).

    `per_action` is `min(horizon, N_v // 4)`; when it is below one the root
    cannot host four disjoint actions and is skipped.  Split actions are omitted
    when `len(root)+d > 64`.
    """
    n_v = 1 << (64 - root.prefixlen)
    per_action = min(horizon, n_v // 4)
    if per_action < 1:
        return [], 0
    used = set()
    probes = []
    for order, child, c64 in sample_sequence(root, per_action, seed, used):
        probes.append(
            make_probe("sample", 0, root, stratum, order, child, c64,
                       probe_iid(c64, seed))
        )
    for d in SPLIT_DEPTHS:
        if root.prefixlen + d > 64:
            continue
        action_id = f"split-{d}"
        children_grid = list(
            ipaddress.ip_network(root).subnets(new_prefix=root.prefixlen + d)
        )
        ranked_children = child_prior(children_grid, descendants, d, seed)
        for order, child, c64 in split_sequence(
            root, d, per_action, seed, used, ranked_children
        ):
            probes.append(
                make_probe(action_id, d, root, stratum, order, child, c64,
                           probe_iid(c64, seed))
            )
    return probes, per_action


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
            pc.project_dir(), "data", "interim", "ris_ipv6_prefixes_unique.csv"
        ),
    )
    parser.add_argument("output_dir")
    parser.add_argument("--exclude-prefix-file")
    parser.add_argument(
        "--split-seed", default="bep-journal-root-split-v1-20260919"
    )
    parser.add_argument("--calibration-root-fraction", type=pc.parse_fraction,
                        default="1/5")
    parser.add_argument(
        "--trace-seed", default="bep-journal-action-trace-v1-20260920"
    )
    parser.add_argument("--roots-per-stratum", type=int, required=True)
    parser.add_argument("--horizon", type=int, default=32)
    args = parser.parse_args(argv)

    if args.roots_per_stratum <= 0:
        parser.error("--roots-per-stratum must be positive")
    if args.horizon <= 0:
        parser.error("--horizon must be positive")

    if not os.path.isfile(args.input):
        print(f"input file does not exist: {args.input}", file=sys.stderr)
        return 2

    try:
        exclusions = pc.load_exclusions(args.exclude_prefix_file)
        metadata, excluded = pc.load_rows(args.input, exclusions)
        prefixes = set(metadata)
        parents = pc.build_immediate_parent_map(prefixes)
        roots = prefixes - set(parents)

        root_cache = {}
        depth_cache = {root: 0 for root in roots}
        root_max_tree_depth = Counter()
        descendants_by_root = defaultdict(list)
        for prefix in sorted(prefixes, key=lambda p: p.prefixlen, reverse=True):
            root = pc.root_for(prefix, parents, root_cache)
            tree_depth = pc.tree_depth_for(prefix, parents, depth_cache)
            root_max_tree_depth[root] = max(root_max_tree_depth[root], tree_depth)
            if prefix != root:
                descendants_by_root[root].append(prefix)

        roots_by_stratum = defaultdict(list)
        for root in roots:
            roots_by_stratum[
                pc.root_depth_stratum(root_max_tree_depth[root])
            ].append(root)

        assignments, _ = pc.assign_root_tranches(
            roots_by_stratum, args.split_seed, args.calibration_root_fraction
        )

        calibration_roots_by_stratum = defaultdict(list)
        for stratum in roots_by_stratum:
            for root in roots_by_stratum[stratum]:
                if assignments[root] == "calibration":
                    calibration_roots_by_stratum[stratum].append(root)

        selected_by_stratum = {}
        for stratum in sorted(calibration_roots_by_stratum):
            ranked = sorted(
                calibration_roots_by_stratum[stratum],
                key=lambda root: pc.hash_int(
                    args.trace_seed, "action-trace-root", stratum, root
                ),
            )
            selected_by_stratum[stratum] = ranked[: args.roots_per_stratum]

        probes = []
        stratum_probe_count = Counter()
        stratum_selected_count = Counter()
        stratum_skipped_count = Counter()
        skipped_roots = []
        for stratum in sorted(selected_by_stratum):
            for root in sorted(
                selected_by_stratum[stratum],
                key=lambda r: (int(r.network_address), r.prefixlen),
            ):
                stratum_selected_count[stratum] += 1
                root_probes, per_action = generate_root_probes(
                    root,
                    stratum,
                    descendants_by_root[root],
                    args.trace_seed,
                    args.horizon,
                )
                if not root_probes:
                    stratum_skipped_count[stratum] += 1
                    skipped_roots.append(
                        {
                            "root_prefix": str(root),
                            "root_stratum": stratum,
                            "reason": "n_v_too_small_for_four_actions",
                            "n_v": 1 << (64 - root.prefixlen),
                        }
                    )
                    continue
                probes.extend(root_probes)
                stratum_probe_count[stratum] += len(root_probes)

        if not probes:
            raise ValueError("no action-trace probes were generated")

        os.makedirs(args.output_dir, exist_ok=True)
        summary_path = os.path.join(args.output_dir, "action_trace_summary.json")
        targets_path = os.path.join(args.output_dir, "action_trace_targets.csv")
        lines_path = os.path.join(args.output_dir, "action_trace_targets.txt")
        if os.path.exists(summary_path):
            raise FileExistsError(
                f"refusing to overwrite existing output: {summary_path}"
            )

        write_csv(targets_path, TRACE_FIELDS, probes)

        ordered = sorted(
            probes,
            key=lambda row: pc.hash_int(
                args.trace_seed, "action-trace-order", row["probe_id"]
            ),
        )
        write_lines(lines_path, [row["target_ipv6"] for row in ordered])

        action_counts = Counter(row["action_id"] for row in probes)
        stratum_action_counts = defaultdict(Counter)
        for row in probes:
            stratum_action_counts[row["root_stratum"]][row["action_id"]] += 1

        summary = {
            "input": os.path.abspath(args.input),
            "exclude_prefix_file": (
                os.path.abspath(args.exclude_prefix_file)
                if args.exclude_prefix_file
                else None
            ),
            "configured_exclusions": [str(p) for p in exclusions],
            "excluded": dict(excluded),
            "split_seed": args.split_seed,
            "calibration_root_fraction": str(args.calibration_root_fraction),
            "trace_seed": args.trace_seed,
            "roots_per_stratum": args.roots_per_stratum,
            "horizon": args.horizon,
            "horizons": list(HORIZONS),
            "split_depths": list(SPLIT_DEPTHS),
            "strata": {
                stratum: {
                    "calibration_root_count": len(calibration_roots_by_stratum[stratum]),
                    "selected_root_count": stratum_selected_count[stratum],
                    "skipped_root_count": stratum_skipped_count[stratum],
                    "probe_count": stratum_probe_count[stratum],
                    "action_counts": dict(stratum_action_counts[stratum]),
                }
                for stratum in sorted(selected_by_stratum)
            },
            "selected_root_count": sum(stratum_selected_count.values()),
            "skipped_root_count": sum(stratum_skipped_count.values()),
            "total_probe_budget": len(probes),
            "action_counts": dict(action_counts),
            "skipped_roots": skipped_roots,
            "outputs": {
                "targets": os.path.abspath(targets_path),
                "target_list": os.path.abspath(lines_path),
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
