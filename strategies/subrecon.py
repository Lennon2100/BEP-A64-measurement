"""BGP-only ICNP SubRecon delimitation, using its published probe table.

The external Hitlist-driven expansion phase is omitted: no activity-bearing
seed is available under the common BGP-only input rule.  Per-prefix aggregates
(sent, replies, distinct AU sources) replace the earlier root-range scans, so
state is O(active prefixes); each /64 is attributed to the exact prefix that
drew it.
"""

import ipaddress
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
        self.frame = frame
        self.targets = targets
        self.allowance = allowance
        self.sent = 0
        self.positive_count = 0
        self.queue = deque(sorted(frame.prefixes, key=lambda p: (p.prefixlen, str(p))))
        self.seen = set(frame.prefixes)
        self.nodes = {}            # prefix -> {"parent", "sent", "replies", "sources"}
        self.native_prefixes = []

    def _activate(self, prefix, parent=None):
        self.nodes[prefix] = {
            "parent": parent, "sent": 0, "replies": 0, "sources": set(), "closed": False
        }

    def _finish(self, prefix):
        rec = self.nodes[prefix]
        quota = PROBE_TABLE[prefix.prefixlen]
        if len(rec["sources"]) == 1 and (prefix.prefixlen == 64 or rec["replies"] > 0.9 * quota):
            self.native_prefixes.append(str(prefix))
        elif prefix.prefixlen < 64 and (
            len(rec["sources"]) > 1 or (len(rec["sources"]) == 1 and rec["replies"] <= 0.9 * quota)
        ):
            for child in prefix.subnets(new_prefix=prefix.prefixlen + 1):
                if child not in self.seen:
                    self.queue.append(child)
                    self.seen.add(child)
                    self._activate(child, prefix)

    def iter_targets(self, limit):
        rounds = len(self.queue)
        while limit > 0 and self.queue and rounds > 0:
            rounds -= 1
            prefix = self.queue.popleft()
            if prefix not in self.nodes:
                self._activate(prefix)
            rec = self.nodes[prefix]
            quota = PROBE_TABLE[prefix.prefixlen]
            if rec["closed"] or rec["sent"] >= quota or len(rec["sources"]) > 1:
                self._finish(prefix)
                continue
            ancestors = []
            parent = rec["parent"]
            while parent is not None:
                ancestors.append(parent)
                parent = self.nodes[parent]["parent"]
            count = min(limit, quota - rec["sent"])
            self.queue.append(prefix)
            for _ in range(count):
                c64 = self.targets.draw(prefix, ancestors)
                if c64 is None:
                    rec["closed"] = True
                    break
                yield (c64, str(prefix), "delimitation")
                limit -= 1

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        rec = self.nodes[ipaddress.ip_network(node_str)]
        rec["sent"] += probes
        rec["replies"] += replies
        rec["sources"].update(sources)
        self.sent += probes
        self.positive_count += positives

    def finish_batch(self):
        pass

    def snapshot(self):
        return {
            "queue": [str(p) for p in self.queue],
            "seen": [str(p) for p in self.seen],
            "nodes": {
                str(p): {
                    "sent": rec["sent"],
                    "parent": str(rec["parent"]) if rec["parent"] else None,
                    "replies": rec["replies"],
                    "sources": sorted(rec["sources"]),
                    "closed": rec["closed"],
                }
                for p, rec in self.nodes.items()
            },
            "native_prefixes": self.native_prefixes,
            "sent": self.sent,
            "positive_count": self.positive_count,
        }

    def restore(self, state):
        self.queue = deque(ipaddress.ip_network(p) for p in state["queue"])
        self.seen = {ipaddress.ip_network(p) for p in state["seen"]}
        self.nodes = {
            ipaddress.ip_network(p): {
                "parent": ipaddress.ip_network(rec["parent"]) if rec["parent"] else None,
                "sent": rec["sent"],
                "replies": rec["replies"],
                "sources": set(rec["sources"]),
                "closed": rec.get("closed", False),
            }
            for p, rec in state["nodes"].items()
        }
        self.native_prefixes = list(state["native_prefixes"])
        self.sent = state["sent"]
        self.positive_count = state["positive_count"]
