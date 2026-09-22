"""Shared routed-frame loading and unique /64 target generation."""

import csv
import ipaddress
import random
from collections import defaultdict

from scripts.count_prefix_nesting import build_immediate_parent_map
from scripts.prepare_campaign import load_exclusions, load_rows, root_for


def network(text):
    value = ipaddress.ip_network(text, strict=True)
    if value.version != 6 or value.prefixlen > 64:
        raise ValueError(f"invalid routed IPv6 prefix: {text}")
    return value


class Frame:
    def __init__(self, prefixes, parents):
        self.prefixes = sorted(prefixes, key=lambda p: (int(p.network_address), p.prefixlen))
        self.roots = sorted(set(prefixes) - set(parents), key=lambda p: int(p.network_address))
        self.prefixes_by_root = defaultdict(list)
        self.descendants = {prefix: 0 for prefix in prefixes}
        root_cache = {}
        for prefix in prefixes:
            self.prefixes_by_root[root_for(prefix, parents, root_cache)].append(prefix)
        for prefix in sorted(prefixes, key=lambda p: p.prefixlen, reverse=True):
            if prefix in parents:
                self.descendants[parents[prefix]] += 1 + self.descendants[prefix]

    @classmethod
    def journal(cls, prefix_csv, frame_exclusions):
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


class Targets:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.used = set()
        self.positions = {}

    def draw(self, prefix):
        count = 1 << (64 - prefix.prefixlen)
        start = int(prefix.network_address) >> 64
        if prefix not in self.positions:
            offset = self.rng.randrange(count)
            stride = 1 if count == 1 else 2 * self.rng.randrange(count // 2) + 1
            self.positions[prefix] = [offset, stride, 0]
        position = self.positions[prefix]
        while position[2] < count:
            value = start + ((position[0] + position[1] * position[2]) % count)
            position[2] += 1
            if value not in self.used:
                self.used.add(value)
                return value
        return None

    def address(self, c64):
        iid = self.rng.randrange(1, 1 << 64)
        return str(ipaddress.IPv6Address((c64 << 64) | iid))
