"""BGP-only ICNP SubRecon delimitation, using its published probe table.

The external Hitlist-driven expansion phase is omitted: no activity-bearing
seed is available under the common BGP-only input rule.
"""

import bisect
from collections import deque

from strategies.common import Frame


def load_frame(cfg, base):
    inputs = cfg["input"]
    return Frame.subrecon(base / inputs["subrecon_prefix_csv"], base / inputs["frame_exclusions"])


# thuname/subrecon src/budget.c, indexed by prefix length 0..64.
PROBE_TABLE = (
    311224, 261707, 220069, 185055, 155613, 130854, 110035, 92528,
    77807, 65428, 55018, 46265, 38902, 32715, 27510, 23133,
    19453, 16358, 13756, 11567, 9727, 8180, 6879, 5784,
    4864, 4091, 3440, 2893, 2433, 2046, 1721, 1447,
    1217, 1024, 861, 724, 609, 513, 431, 363,
    303, 255, 216, 180, 151, 129, 109, 92,
    77, 65, 55, 47, 39, 33, 28, 24,
    20, 128, 64, 32, 16, 8, 4, 2, 1,
)


class Strategy:
    name = "subrecon"

    def __init__(self, frame, targets, cfg, allowance):
        self.targets = targets
        self.queue = deque(sorted(frame.prefixes, key=lambda p: (p.prefixlen, str(p))))
        self.records_by_root = {root: {} for root in frame.roots}
        self.roots = frame.roots
        self.root_starts = [int(root.network_address) >> 64 for root in frame.roots]
        self.pending = []
        self.native_prefixes = []
        self.seen_nodes = set(frame.prefixes)

    def _rows_in(self, prefix):
        start = int(prefix.network_address) >> 64
        end = start + (1 << (64 - prefix.prefixlen))
        root_index = bisect.bisect_right(self.root_starts, start) - 1
        return [row for c64, row in self.records_by_root[self.roots[root_index]].items() if start <= c64 < end]

    def _finish(self, prefix):
        rows = self._rows_in(prefix)
        sources = {row["icmp_source"] for row in rows if row["response_class"] in ("slow_au", "fast_au") and row["icmp_source"]}
        replies = sum(row["response_class"] in ("slow_au", "fast_au", "direct") for row in rows)
        quota = PROBE_TABLE[prefix.prefixlen]
        if len(sources) == 1 and (prefix.prefixlen == 64 or replies > 0.9 * quota):
            self.native_prefixes.append(str(prefix))
        elif prefix.prefixlen < 64 and (len(sources) > 1 or (len(sources) == 1 and replies <= 0.9 * quota)):
            for child in prefix.subnets(new_prefix=prefix.prefixlen + 1):
                if child not in self.seen_nodes:
                    self.queue.append(child)
                    self.seen_nodes.add(child)

    def next_batch(self, limit):
        if self.pending:
            raise ValueError("feedback required before next batch")
        batch = []
        selected_nodes = set()
        while self.queue and len(batch) < limit:
            prefix = self.queue.popleft()
            if prefix in selected_nodes:
                self.queue.appendleft(prefix)
                break
            selected_nodes.add(prefix)
            rows = self._rows_in(prefix)
            sources = {row["icmp_source"] for row in rows if row["response_class"] in ("slow_au", "fast_au") and row["icmp_source"]}
            remaining = PROBE_TABLE[prefix.prefixlen] - len(rows)
            if len(sources) > 1 or remaining <= 0:
                self._finish(prefix)
                continue
            c64 = self.targets.draw(prefix)
            if c64 is None:
                self._finish(prefix)
                continue
            batch.append({"c64": c64, "node": str(prefix), "stage": "delimitation"})
            self.queue.append(prefix)
        self.pending = batch
        return batch

    def feedback(self, rows):
        if len(rows) != len(self.pending):
            raise ValueError("SubRecon feedback size mismatch")
        for item, row in zip(self.pending, rows):
            index = bisect.bisect_right(self.root_starts, item["c64"]) - 1
            self.records_by_root[self.roots[index]][item["c64"]] = row
        self.pending = []
