"""BEP-derived HD base allocation with lazy, mixed-depth Bayesian search."""

import math
import ipaddress
from collections import defaultdict


class Strategy:
    name = "journal"

    def __init__(self, frame, targets, cfg, allowance):
        self.frame, self.targets, self.cfg = frame, targets, cfg
        self.allowance = allowance
        self.theta = float(cfg["theta_b"])
        self.steps = tuple(cfg["step_bits"])
        self.info_weight = float(cfg["information_weight"])
        self.prior_strength = float(cfg["prior_strength"])
        self.prior_decay = float(cfg["prior_decay"])
        self.bgp_weight = float(cfg["bgp_weight"])
        self.root_fraction = float(cfg["root_budget_fraction"])
        if not (self.theta > 0 and self.prior_strength > 0 and 0 < self.prior_decay <= 1 and self.info_weight >= 0 and self.bgp_weight >= 0 and 0 < self.root_fraction < 1 and self.steps and all(1 <= step <= 8 for step in self.steps)):
            raise ValueError("invalid journal search coefficients or step_bits")
        self.observations = {}
        self.records_by_root = defaultdict(dict)
        self.active = {}
        self.closed = set()
        self.pending = []
        self.root_order = sorted(frame.roots, key=lambda p: (-frame.descendants[p], str(p)))
        self.root_index = 0

    def base(self, prefix):
        if prefix.prefixlen == 64:
            return 1
        count = 1 << (64 - prefix.prefixlen)
        return min(count, max(1, math.ceil(self.theta * 2 ** ((56 - prefix.prefixlen) / 4))))

    def _counts(self, prefix):
        start = int(prefix.network_address) >> 64
        stop = start + (1 << (64 - prefix.prefixlen))
        root = self.active[prefix]["root"]
        values = [y for c64, y in self.records_by_root[root].items() if start <= c64 < stop]
        return sum(values), len(values) - sum(values), len(values)

    def _prior_count(self, prefix, root, batch):
        start = int(prefix.network_address) >> 64
        stop = start + (1 << (64 - prefix.prefixlen))
        return sum(start <= c64 < stop for c64 in self.records_by_root[root]) + sum(start <= row["c64"] < stop for row in batch)

    def _posterior(self, prefix):
        parent = self.active[prefix]["parent"]
        if parent is None:
            a0 = b0 = self.prior_strength / 2
        else:
            pa, pb = self._posterior(parent)
            own_yes, own_no, _ = self._counts(prefix)
            pa, pb = max(pa - own_yes, 0.01), max(pb - own_no, 0.01)
            mean = pa / (pa + pb)
            strength = max(0.02, self.prior_strength * self.prior_decay ** (prefix.prefixlen - parent.prefixlen))
            a0, b0 = mean * strength, (1 - mean) * strength
        yes, no, _ = self._counts(prefix)
        return a0 + yes, b0 + no

    def _score(self, prefix):
        a, b = self._posterior(prefix)
        p = a / (a + b)
        def entropy(q):
            return 0 if q in (0, 1) else -q * math.log(q) - (1 - q) * math.log(1 - q)
        information = entropy(p) - p * entropy((a + 1) / (a + b + 1)) - (1 - p) * entropy(a / (a + b + 1))
        return p + self.info_weight * information

    def _choose_child(self, prefix):
        options = []
        for step in self.steps:
            length = prefix.prefixlen + step
            if length > 64:
                continue
            # Prefer the subspace with the deepest BGP evidence, then a
            # response-blind random subspace when no more-specific exists.
            root = self.active[prefix]["root"]
            bgp_prefixes = getattr(self.frame, "prefixes_by_root", {}).get(root, self.frame.prefixes)
            descendants = sorted((p for p in bgp_prefixes if p != prefix and p.subnet_of(prefix) and p.prefixlen >= length), key=lambda p: (p.prefixlen, self.frame.descendants[p]), reverse=True)
            child = None
            evidence = 1
            for ranked in descendants:
                proposed = ranked.supernet(new_prefix=length) if ranked.prefixlen > length else ranked
                if proposed not in self.active:
                    child = proposed
                    evidence += self.bgp_weight * math.log1p(self.frame.descendants[ranked] + 1)
                    break
            if child is None:
                children = list(prefix.subnets(new_prefix=length))
                self.targets.rng.shuffle(children)
                child = next((part for part in children if part not in self.active), None)
            if child is not None and child not in self.active:
                options.append((self._score(prefix) * evidence, child))
        return max(options, default=(0, None), key=lambda item: item[0])

    def next_batch(self, limit):
        if self.pending:
            raise ValueError("feedback required before next batch")
        batch = []
        planned_counts = defaultdict(int)
        while len(batch) < limit:
            candidates = [p for p in self.active if p not in self.closed and self._counts(p)[2] + planned_counts[p] < self.base(p)]
            if candidates:
                node = max(candidates, key=self._score)
            elif self.root_index < len(self.root_order) and len(self.observations) + len(batch) < self.allowance * self.root_fraction:
                node = self.root_order[self.root_index]
                self.root_index += 1
                if self.base(node) > self.allowance - len(self.observations) - len(batch):
                    continue
                self.active[node] = {"parent": None, "root": node}
            else:
                expandable = [p for p in self.active if p not in self.closed and p.prefixlen < 64]
                if not expandable:
                    break
                parent = max(expandable, key=self._score)
                child_score, child = self._choose_child(parent)
                unsatisfied = max(0, self.base(child) - self._prior_count(child, self.active[parent]["root"], batch)) if child else 0
                if child is not None and child_score > self._score(parent) and unsatisfied <= self.allowance - len(self.observations) - len(batch):
                    self.active[child] = {"parent": parent, "root": self.active[parent]["root"]}
                    planned_counts[child] = sum(ipaddress.IPv6Address(item["c64"] << 64) in child for item in batch)
                    node = child
                else:
                    node = parent
            c64 = self.targets.draw(node)
            if c64 is None:
                self.closed.add(node)
                continue
            batch.append({"c64": c64, "node": str(node), "stage": "search"})
            current = node
            while current is not None:
                planned_counts[current] += 1
                current = self.active[current]["parent"]
        self.pending = batch
        return batch

    def feedback(self, rows):
        if len(rows) != len(self.pending):
            raise ValueError("journal feedback size mismatch")
        for item, row in zip(self.pending, rows):
            yes = int(row["is_observed_positive"])
            self.observations[item["c64"]] = yes
            node = ipaddress.ip_network(item["node"])
            self.records_by_root[self.active[node]["root"]][item["c64"]] = yes
        self.pending = []
