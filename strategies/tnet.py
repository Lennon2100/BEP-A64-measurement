"""Paper-based TNet adaptation: charged /48 screen, then /48-/52 feedback."""

import bisect
import ipaddress
import math
from collections import Counter, defaultdict
from itertools import accumulate


class Strategy:
    name = "tnet"

    def __init__(self, frame, targets, cfg, allowance):
        self.frame, self.targets, self.cfg = frame, targets, cfg
        self.allowance = allowance
        self.screen_budget = min(int(cfg["screen_budget"]), allowance)
        self.rounds = int(cfg["rounds"])
        self.top_k = int(cfg["top_k"])
        self.temperature = float(cfg["temperature"])
        if self.rounds <= 0 or self.top_k <= 0 or self.temperature <= 0:
            raise ValueError("TNet rounds, top_k, and temperature must be positive")
        self.blocks = []
        self.ends = []
        total = 0
        for root in frame.roots:
            count = 1 << (48 - root.prefixlen) if root.prefixlen < 48 else 1
            self.blocks.append((root, count))
            total += count
            self.ends.append(total)
        self.screen_ordinals = iter(self.targets.rng.sample(range(total), min(total, self.screen_budget)))
        self.screen_sent = 0
        self.screen = defaultdict(list)
        self.regions = []
        self.counts = defaultdict(Counter)
        self.pending = []
        self.search_sent = 0
        self.candidate_round = 0
        self.candidates = []
        self.candidate_cumulative = []

    def _region(self, ordinal):
        index = bisect.bisect_right(self.ends, ordinal)
        root, count = self.blocks[index]
        offset = ordinal - (self.ends[index - 1] if index else 0)
        if root.prefixlen >= 48:
            return root
        return ipaddress.ip_network((int(root.network_address) + (offset << 80), 48))

    def _finish_screen(self):
        source_counts = Counter(
            r["icmp_source"]
            for rows in self.screen.values()
            for r in rows
            if r["response_class"] in ("slow_au", "fast_au") and r["icmp_source"]
        )
        for region, rows in self.screen.items():
            source = next((r["icmp_source"] for r in rows if r["response_class"] in ("slow_au", "fast_au") and r["icmp_source"]), None)
            if source and source_counts[source] == 1:
                self.regions.append(region)
            elif any(r["response_class"] == "direct" for r in rows):
                self.regions.append(region)
        self.regions.sort(key=str)

    def _pick_region(self):
        if not self.regions:
            return None
        round_index = min(self.rounds, 1 + self.search_sent * self.rounds // max(1, self.allowance - self.screen_budget))
        if round_index != self.candidate_round or not self.candidates:
            ranked = sorted(self.regions, key=lambda p: (self.counts[p]["hits"] / max(1, self.counts[p]["sent"]), str(p)), reverse=True)
            self.candidates = self.regions[:] if round_index == 1 else ranked[:self.top_k]
            temperature = self.temperature / round_index
            weights = [math.exp(min(30, self.counts[p]["hits"] / max(1, self.counts[p]["sent"]) / temperature)) for p in self.candidates]
            self.candidate_cumulative = list(accumulate(weights))
            self.candidate_round = round_index
        index = bisect.bisect_right(self.candidate_cumulative, self.targets.rng.random() * self.candidate_cumulative[-1])
        return self.candidates[index]

    def next_batch(self, limit):
        if self.pending:
            raise ValueError("feedback required before next batch")
        batch = []
        while len(batch) < limit:
            if self.screen_sent < self.screen_budget:
                try:
                    region = self._region(next(self.screen_ordinals))
                except StopIteration:
                    self.screen_sent = self.screen_budget
                    continue
                c64 = self.targets.draw(region)
                self.screen_sent += 1
                if c64 is None:
                    continue
                batch.append({"c64": c64, "node": str(region), "stage": "screen"})
            else:
                if batch:
                    break  # screening feedback must precede adaptive search
                if not self.regions:
                    self._finish_screen()
                region = self._pick_region()
                if region is None:
                    break
                if region.prefixlen <= 48:
                    children = list(region.subnets(new_prefix=52))
                    if self.candidate_round == 1:
                        subregion = children[self.targets.rng.randrange(16)]
                    else:
                        temperature = self.temperature / min(self.rounds, 1 + self.search_sent * self.rounds // max(1, self.allowance - self.screen_budget))
                        weights = [math.exp(min(30, self.counts[p]["hits"] / max(1, self.counts[p]["sent"]) / temperature)) for p in children]
                        subregion = self.targets.rng.choices(children, weights=weights)[0]
                else:
                    subregion = region
                c64 = self.targets.draw(subregion)
                if c64 is None:
                    self.regions.remove(region)
                    self.candidate_round = 0
                    continue
                batch.append({"c64": c64, "node": str(subregion), "stage": "search"})
                self.search_sent += 1
        self.pending = batch
        return batch

    def feedback(self, rows):
        if len(rows) != len(self.pending):
            raise ValueError("TNet feedback size mismatch")
        for item, row in zip(self.pending, rows):
            region = ipaddress.ip_network(item["node"], strict=True)
            if item["stage"] == "screen":
                self.screen[region].append(row)
            else:
                parent = region.supernet(new_prefix=48) if region.prefixlen == 52 else region
                for key in (parent, region):
                    self.counts[key]["sent"] += 1
                    self.counts[key]["hits"] += int(row["is_observed_positive"])
        self.pending = []
