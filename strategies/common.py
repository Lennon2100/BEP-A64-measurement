"""Shared target generation and BGP frame for the three formal strategies."""

import csv
import ipaddress
import random
from collections import defaultdict


def network(text):
    value = ipaddress.ip_network(text, strict=True)
    if value.version != 6 or value.prefixlen > 64:
        raise ValueError(f"invalid routed IPv6 prefix: {text}")
    return value


def c64_of(address):
    return ipaddress.ip_network((int(ipaddress.ip_address(address)), 64), strict=False)


class Frame:
    def __init__(self, tree_csv):
        self.roots = []
        self.prefixes = []
        self.prefixes_by_root = defaultdict(list)
        self.descendants = {}
        with open(tree_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                prefix = network(row["prefix"])
                root = network(row["root_prefix"])
                if prefix in self.descendants or not prefix.subnet_of(root):
                    raise ValueError(f"duplicate or inconsistent BGP prefix: {prefix}")
                self.prefixes.append(prefix)
                self.prefixes_by_root[root].append(prefix)
                self.descendants[prefix] = int(row["descendant_prefix_count"])
                parent = row["parent_prefix"]
                if parent:
                    parent_prefix = network(parent)
                    if parent_prefix == prefix or not prefix.subnet_of(parent_prefix):
                        raise ValueError(f"invalid BGP parent for {prefix}: {parent}")
                else:
                    if prefix != root:
                        raise ValueError(f"invalid BGP root for {prefix}: {root}")
                    self.roots.append(prefix)
        if not self.roots:
            raise ValueError("BGP tree has no roots")
        self.roots.sort(key=lambda p: int(p.network_address))


class Targets:
    def __init__(self, seed, exclusions=(), prior_c64s=()):
        self.rng = random.Random(seed)
        self.exclusions = tuple(exclusions)
        self.used = set(prior_c64s)

    def draw(self, prefix):
        count = 1 << (64 - prefix.prefixlen)
        start = int(prefix.network_address) >> 64
        # The sequence is uniform without replacement within this request.
        # A nearly exhausted node is walked deterministically at the end.
        for _ in range(min(64, count)):
            value = start + self.rng.randrange(count)
            if value not in self.used and self._allowed(value):
                self.used.add(value)
                return value
        if count <= 65536:
            offset = self.rng.randrange(count)
            for step in range(count):
                value = start + (offset + step) % count
                if value not in self.used and self._allowed(value):
                    self.used.add(value)
                    return value
        return None

    def _allowed(self, c64):
        prefix64 = ipaddress.ip_network((c64 << 64, 64))
        return all(not prefix64.subnet_of(exclusion) for exclusion in self.exclusions)

    def address(self, c64):
        # One fresh IID per formal /64, shared policy across methods.
        for _ in range(64):
            iid = self.rng.randrange(1, 1 << 64)
            address = ipaddress.IPv6Address((c64 << 64) | iid)
            if all(address not in prefix for prefix in self.exclusions):
                return str(address)
        raise ValueError(f"cannot choose an allowed IID in {ipaddress.ip_network((c64 << 64, 64))}")


def read_prefixes(path):
    with open(path, encoding="utf-8") as fh:
        prefixes = [ipaddress.ip_network(line.split("#", 1)[0].strip(), strict=False) for line in fh if line.split("#", 1)[0].strip()]
    if any(prefix.version != 6 for prefix in prefixes):
        raise ValueError(f"scan exclusions include a non-IPv6 prefix: {path}")
    return prefixes


def read_prior_c64s(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {int(c64_of(row["c64"]).network_address) >> 64 for row in csv.DictReader(fh) if row["round"] == "search"}
