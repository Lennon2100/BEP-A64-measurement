"""Shared routed-frame loading and unique /64 target generation.

A target is a /64 candidate; its full IPv6 address is the /64 prefix plus one
random interface identifier.  Each prefix keeps a deterministic full-cycle
permutation over its own /64 space.  Descendants reject candidates that occur
before an ancestor's saved permutation cursor.  This gives exact cross-level
deduplication from O(nodes) cursor state; strategies stop drawing from a parent
after activating descendants.

The generator state (`rng` + per-prefix permutation cursors) is snapshot/restore
compatible so a run can resume without replaying completed batches.
"""

import bisect
import csv
import heapq
import ipaddress
import random
from array import array
from collections import defaultdict
from dataclasses import dataclass

from scripts.prefix_data import (
    build_immediate_parent_map,
    load_exclusions,
    load_rows,
    root_for,
)


def network(text):
    value = ipaddress.ip_network(text, strict=True)
    if value.version != 6 or value.prefixlen > 64:
        raise ValueError(f"invalid routed IPv6 prefix: {text}")
    return value


class Frame:
    """Immutable routed BGP frame: prefixes, non-overlapping roots, tree features."""

    def __init__(self, prefixes, parents):
        self.prefix_set = set(prefixes)
        self.children = defaultdict(list)
        for child, parent in parents.items():
            self.children[parent].append(child)
        for children in self.children.values():
            children.sort(key=lambda p: (p.prefixlen, int(p.network_address)))
        self.prefixes = sorted(prefixes, key=lambda p: (int(p.network_address), p.prefixlen))
        self.roots = sorted(set(prefixes) - set(parents), key=lambda p: int(p.network_address))
        self.prefixes_by_root = defaultdict(list)
        self.descendants = {prefix: 0 for prefix in prefixes}
        self.deepest = {prefix: prefix.prefixlen for prefix in prefixes}
        root_cache = {}
        for prefix in prefixes:
            self.prefixes_by_root[root_for(prefix, parents, root_cache)].append(prefix)
        for prefix in sorted(prefixes, key=lambda p: p.prefixlen, reverse=True):
            if prefix in parents:
                parent = parents[prefix]
                self.descendants[parent] += 1 + self.descendants[prefix]
                self.deepest[parent] = max(self.deepest[parent], self.deepest[prefix])

    @classmethod
    def from_bgp_prefixes(cls, prefix_csv, frame_exclusions):
        rows, _ = load_rows(prefix_csv, load_exclusions(frame_exclusions))
        prefixes = set(rows)
        return cls(prefixes, build_immediate_parent_map(prefixes))

    @classmethod
    def subrecon(cls, top_level_csv, frame_exclusions):
        exclusions = load_exclusions(frame_exclusions)
        prefixes = set()
        with open(top_level_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                prefix = network(row["prefix"])
                if not any(prefix.subnet_of(exclusion) for exclusion in exclusions):
                    prefixes.add(prefix)
        return cls(prefixes, {})


@dataclass(slots=True)
class Position:
    offset: int
    stride: int
    cursor: int = 0


class Targets:
    """Deterministic, resume-compatible /64 target generator with exact dedup."""

    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        self.positions = {}        # prefix -> Position
        self.recovered = array("Q")  # sorted exceptional legacy-recovery /64s
        self.recovery_batches = []

    def _position(self, prefix):
        count = 1 << (64 - prefix.prefixlen)
        if prefix not in self.positions:
            offset = self.rng.randrange(count)
            stride = 1 if count == 1 else 2 * self.rng.randrange(count // 2) + 1
            self.positions[prefix] = Position(offset, stride)
        return self.positions[prefix]

    def was_drawn(self, prefix, c64):
        position = self.positions.get(prefix)
        if position is None:
            return False
        count = 1 << (64 - prefix.prefixlen)
        start = int(prefix.network_address) >> 64
        relative = c64 - start
        if not 0 <= relative < count:
            return False
        index = 0 if count == 1 else (
            (relative - position.offset) * pow(position.stride, -1, count)
        ) % count
        return index < position.cursor

    def drawn_values(self, prefix):
        """Yield the /64 indices covered before this node's saved cursor."""
        position = self.positions.get(prefix)
        if position is None:
            return
        count = 1 << (64 - prefix.prefixlen)
        start = int(prefix.network_address) >> 64
        for index in range(position.cursor):
            yield start + ((position.offset + position.stride * index) % count)

    def prior_count(self, prefix, ancestors):
        """Count distinct earlier targets inside a newly activated descendant."""
        start = int(prefix.network_address) >> 64
        stop = start + (1 << (64 - prefix.prefixlen))
        prior = {
            value
            for ancestor in ancestors
            for value in self.drawn_values(ancestor)
            if start <= value < stop
        }
        prior.update(self.recovered[bisect.bisect_left(self.recovered, start):bisect.bisect_left(self.recovered, stop)])
        return len(prior)

    def draw(self, prefix, ancestors=()):
        """Return the next unprobed /64 in `prefix`'s permutation, or None."""
        count = 1 << (64 - prefix.prefixlen)
        start = int(prefix.network_address) >> 64
        position = self._position(prefix)
        while position.cursor < count:
            value = start + ((position.offset + position.stride * position.cursor) % count)
            position.cursor += 1
            recovered_at = bisect.bisect_left(self.recovered, value)
            was_recovered = recovered_at < len(self.recovered) and self.recovered[recovered_at] == value
            if not was_recovered and not any(self.was_drawn(ancestor, value) for ancestor in ancestors):
                return value
        return None

    def address(self, c64):
        iid = self.rng.randrange(1, 1 << 64)
        return str(ipaddress.IPv6Address((c64 << 64) | iid))

    def forget(self, prefix):
        self.positions.pop(prefix, None)

    def snapshot(self):
        """Compact generator state: RNG plus one permutation cursor per node."""
        return {
            "seed": self.seed,
            "rng": self.rng.getstate(),
            "positions": {
                str(prefix): [position.offset, position.stride, position.cursor]
                for prefix, position in self.positions.items()
            },
            "recovery_batches": self.recovery_batches,
        }

    def checkpoint(self):
        """Return live compact state without copying the cursor table."""
        return {
            "native": True,
            "seed": self.seed,
            "rng": self.rng.getstate(),
            "positions": self.positions,
            "recovery_batches": self.recovery_batches,
        }

    def restore(self, state):
        if state["seed"] != self.seed:
            raise ValueError("target generator seed mismatch")
        if state.get("native"):
            self.rng.setstate(state["rng"])
            positions = state["positions"]
            if positions and not isinstance(next(iter(positions.values())), Position):
                for prefix, position in positions.items():
                    positions[prefix] = Position(*position)
            self.positions = positions
            self.recovered = array("Q")
            self.recovery_batches = state.get("recovery_batches", [])
            return
        def tuples(value):
            return tuple(tuples(item) for item in value) if isinstance(value, list) else value

        self.rng.setstate(tuples(state["rng"]))
        self.positions = {
            ipaddress.ip_network(prefix): Position(*position)
            for prefix, position in state["positions"].items()
        }
        self.recovered = array("Q")
        self.recovery_batches = list(state.get("recovery_batches", []))

    def add_recovered(self, c64_values, batch_number, reseed=True):
        recovered_batch = array("Q", c64_values)
        if self.recovered:
            merged = array("Q")
            previous = None
            for c64 in heapq.merge(self.recovered, recovered_batch):
                if c64 != previous:
                    merged.append(c64)
                    previous = c64
            self.recovered = merged
        else:
            self.recovered = recovered_batch
        if batch_number not in self.recovery_batches:
            self.recovery_batches.append(batch_number)
        if reseed:
            self.rng = random.Random(f"{self.seed}:recovery:{batch_number}")
