"""BEP-derived HD base allocation with lazy, mixed-depth Bayesian search.

Block-level scheduling: one feedback round decides a plan of `(node, count)`
actions in a single frontier pass, then streams targets.  No per-probe
re-scoring.  Every probe is drawn uniformly within exactly one node (the
action's node) and updates only that node's Beta counts; a probe drawn in a
child is uniform in the child and is not a uniform sample of its ancestors.
Cross-level reuse is automatic: a descendant's permutation skips /64s already
drawn by an ancestor (dedup), so an ancestor's higher-level uniform probe is
never re-sent and reduces the descendant's unsatisfied base by exactly the
number of already-probed /64s that fall in it.

State is O(active nodes); cross-level dedup is cursor-based (see Targets).  A
"search layer" is one frontier scheduling round over nodes of possibly
different absolute lengths.
"""

import heapq
import ipaddress
import math

from strategies.common import Frame


def _entropy(q):
    return 0.0 if q in (0.0, 1.0) else -q * math.log(q) - (1.0 - q) * math.log(1.0 - q)


def load_frame(cfg, base):
    inputs = cfg["input"]
    return Frame.journal(base / inputs["journal_prefix_csv"], base / inputs["frame_exclusions"])


class Strategy:
    name = "journal"

    def __init__(self, frame, targets, cfg, allowance):
        self.frame = frame
        self.targets = targets
        self.allowance = allowance
        self.theta = float(cfg["theta_b"])
        self.steps = tuple(cfg["step_bits"])
        self.info_weight = float(cfg["information_weight"])
        self.prior_strength = float(cfg["prior_strength"])
        self.prior_decay = float(cfg["prior_decay"])
        self.bgp_weight = float(cfg["bgp_weight"])
        self.root_fraction = float(cfg["root_budget_fraction"])
        self.action_block = int(cfg["action_block"])
        if not (
            self.theta > 0
            and self.prior_strength > 0
            and 0 < self.prior_decay <= 1
            and self.info_weight >= 0
            and self.bgp_weight >= 0
            and 0 < self.root_fraction < 1
            and self.action_block > 0
            and self.steps
            and all(1 <= step <= 8 for step in self.steps)
        ):
            raise ValueError("invalid journal search coefficients, action_block, or step_bits")

        # Base and adaptive samples are kept separately.  Both are uniform in
        # this node and therefore both are valid for its local likelihood.
        self.nodes = {}
        self.cache = {}          # prefix -> (alpha, beta, epoch) posterior memo
        self.epoch = 0
        self.heap = []           # (-score, seq, prefix) lazy-max heap
        self.seq = 0
        self.sent = 0
        self.positive_count = 0
        self.root_order = sorted(frame.roots, key=lambda p: (-frame.descendants[p], str(p)))
        self.root_index = 0
        self.root_phase_budget = allowance * self.root_fraction
        self._touched = set()

    # ---- quota and posterior -------------------------------------------------

    def base(self, prefix):
        if prefix.prefixlen == 64:
            return 1
        count = 1 << (64 - prefix.prefixlen)
        return min(count, max(1, math.ceil(self.theta * 2 ** ((56 - prefix.prefixlen) / 4))))

    def _posterior(self, prefix):
        rec = self.nodes[prefix]
        hit = self.cache.get(prefix)
        if hit is not None and hit[2] == self.epoch:
            return hit[0], hit[1]
        if rec["parent"] is None:
            a0 = b0 = self.prior_strength / 2
        else:
            pa, pb = self._posterior(rec["parent"])
            mean = pa / (pa + pb)
            strength = max(
                0.02,
                self.prior_strength
                * self.prior_decay ** (prefix.prefixlen - rec["parent"].prefixlen),
            )
            a0, b0 = mean * strength, (1 - mean) * strength
        alpha = rec["base_yes"] + rec["adaptive_yes"] + a0
        beta = rec["base_no"] + rec["adaptive_no"] + b0
        self.cache[prefix] = (alpha, beta, self.epoch)
        return alpha, beta

    def _score(self, prefix):
        a, b = self._posterior(prefix)
        p = a / (a + b)
        information = _entropy(p) - p * _entropy((a + 1) / (a + b + 1)) - (1 - p) * _entropy(a / (a + b + 1))
        return p + self.info_weight * information

    # ---- activation and child selection --------------------------------------

    def _activate(self, prefix, parent):
        ancestors = []
        ancestor = parent
        while ancestor is not None:
            ancestors.append(ancestor)
            ancestor = self.nodes[ancestor]["parent"]
        quota = max(0, self.base(prefix) - self.targets.prior_count(prefix, ancestors))
        self.nodes[prefix] = {
            "parent": parent,
            "root": prefix if parent is None else self.nodes[parent]["root"],
            "quota": quota,
            "base_yes": 0,
            "base_no": 0,
            "adaptive_yes": 0,
            "adaptive_no": 0,
            "closed": False,
            "heap_score": None,
        }

    def _choose_child(self, prefix):
        rec = self.nodes[prefix]
        bgp_prefixes = self.frame.prefixes_by_root[rec["root"]]
        best_child = None
        best_score = float("-inf")
        for step in self.steps:
            length = prefix.prefixlen + step
            if length > 64:
                continue
            descendants = sorted(
                (
                    p
                    for p in bgp_prefixes
                    if p != prefix and p.subnet_of(prefix) and p.prefixlen >= length
                ),
                key=lambda p: (p.prefixlen, self.frame.descendants[p]),
                reverse=True,
            )
            child = None
            evidence = 1.0
            for ranked in descendants:
                proposed = ranked.supernet(new_prefix=length) if ranked.prefixlen > length else ranked
                if proposed not in self.nodes:
                    child = proposed
                    evidence += self.bgp_weight * math.log1p(self.frame.descendants[ranked] + 1)
                    break
            if child is None:
                children = list(prefix.subnets(new_prefix=length))
                self.targets.rng.shuffle(children)
                child = next((part for part in children if part not in self.nodes), None)
            if child is not None:
                score = self._score(prefix) * evidence
                if score > best_score:
                    best_score = score
                    best_child = child
        return best_child

    # ---- frontier heap --------------------------------------------------------

    def _heap_push(self, prefix):
        score = self._score(prefix)
        self.nodes[prefix]["heap_score"] = score
        heapq.heappush(self.heap, (-score, self.seq, prefix))
        self.seq += 1

    def _heap_pop(self):
        while self.heap:
            _, _, prefix = heapq.heappop(self.heap)
            rec = self.nodes.get(prefix)
            if rec is None or rec["closed"]:
                continue
            if prefix.prefixlen == 64 and self._node_sent(rec) >= rec["quota"]:
                continue
            score = self._score(prefix)
            if rec["heap_score"] is not None and abs(score - rec["heap_score"]) < 1e-12:
                return prefix
            rec["heap_score"] = score
            heapq.heappush(self.heap, (-score, self.seq, prefix))
            self.seq += 1
        return None

    # ---- planning and streaming ----------------------------------------------

    def _plan(self, limit):
        plan = []
        remaining = limit
        planned = self.sent
        while remaining > 0 and self.root_index < len(self.root_order) and planned < self.root_phase_budget:
            root = self.root_order[self.root_index]
            self.root_index += 1
            quota = self.base(root)
            if quota > self.allowance - planned:
                continue  # this root's full base no longer fits; skip it for good
            self._activate(root, None)
            count = min(remaining, quota)
            plan.append((root, count, "base"))
            self._touched.add(root)
            planned += count
            remaining -= count
        while remaining > 0:
            node = self._heap_pop()
            if node is None:
                break
            rec = self.nodes[node]
            sent = self._node_sent(rec)
            if sent < rec["quota"]:
                count = min(remaining, rec["quota"] - sent)
                mode = "base"
            else:
                child = self._choose_child(node)
                if child is None:
                    # All step-children are active, so this node's /64s are
                    # covered by them; drawing here would re-probe descendants.
                    self.nodes[node]["closed"] = True
                    continue
                self._activate(child, node)
                unsatisfied = self.nodes[child]["quota"]
                mode = "base" if unsatisfied else "dynamic"
                node = child
                count = min(remaining, unsatisfied or self.action_block)
            plan.append((node, count, mode))
            self._touched.add(node)
            remaining -= count
        return plan

    def iter_targets(self, limit):
        for node, count, mode in self._plan(limit):
            ancestors = []
            parent = self.nodes[node]["parent"]
            while parent is not None:
                ancestors.append(parent)
                parent = self.nodes[parent]["parent"]
            for _ in range(count):
                c64 = self.targets.draw(node, ancestors)
                if c64 is None:
                    self.nodes[node]["closed"] = True
                    break
                yield (c64, str(node), mode)

    # ---- feedback and persistence --------------------------------------------

    @staticmethod
    def _node_sent(rec):
        return rec["base_yes"] + rec["base_no"] + rec["adaptive_yes"] + rec["adaptive_no"]

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        node = ipaddress.ip_network(node_str)
        stem = "base" if mode == "base" else "adaptive"
        self.nodes[node][f"{stem}_yes"] += positives
        self.nodes[node][f"{stem}_no"] += probes - positives
        self.sent += probes
        self.positive_count += positives

    def finish_batch(self):
        for node in self._touched:
            rec = self.nodes[node]
            if rec["closed"]:
                continue
            if node.prefixlen == 64 and self._node_sent(rec) >= rec["quota"]:
                continue
            self._heap_push(node)
        self._touched.clear()
        self.epoch += 1

    def adopt_manifest_actions(self, actions):
        root_indexes = {root: index for index, root in enumerate(self.root_order)}
        for node_str, _ in actions:
            node = ipaddress.ip_network(node_str)
            if node not in self.nodes:
                parent = next(
                    (
                        node.supernet(new_prefix=length)
                        for length in range(node.prefixlen - 1, -1, -1)
                        if node.supernet(new_prefix=length) in self.nodes
                    ),
                    None,
                )
                self._activate(node, parent)
            if node in root_indexes:
                self.root_index = max(self.root_index, root_indexes[node] + 1)
            self._touched.add(node)

    def rebuild_after_recovery(self):
        parents = {rec["parent"] for rec in self.nodes.values() if rec["parent"] is not None}
        self.heap = []
        self.seq = 0
        for node, rec in self.nodes.items():
            if rec["closed"] or node in parents:
                continue
            if node.prefixlen == 64 and self._node_sent(rec) >= rec["quota"]:
                continue
            self._heap_push(node)

    def snapshot(self):
        return {
            "nodes": {
                str(p): {
                    "parent": str(rec["parent"]) if rec["parent"] else None,
                    "quota": rec["quota"],
                    "root": str(rec["root"]),
                    "base_yes": rec["base_yes"],
                    "base_no": rec["base_no"],
                    "adaptive_yes": rec["adaptive_yes"],
                    "adaptive_no": rec["adaptive_no"],
                    "closed": rec["closed"],
                }
                for p, rec in self.nodes.items()
            },
            "sent": self.sent,
            "positive_count": self.positive_count,
            "root_index": self.root_index,
            "epoch": self.epoch,
            "heap": [[score, seq, str(node)] for score, seq, node in self.heap],
            "seq": self.seq,
            "touched": [str(node) for node in self._touched],
        }

    def checkpoint(self):
        """Return live node state for bounded-overhead checkpoint serialization."""
        return {
            "native": True,
            "nodes": self.nodes,
            "sent": self.sent,
            "positive_count": self.positive_count,
            "root_index": self.root_index,
            "epoch": self.epoch,
            "heap": self.heap,
            "seq": self.seq,
            "touched": self._touched,
        }

    def restore(self, state):
        if state.get("native"):
            self.nodes = state["nodes"]
            self.sent = state["sent"]
            self.positive_count = state["positive_count"]
            self.root_index = state["root_index"]
            self.epoch = state["epoch"]
            self.heap = state["heap"]
            self.seq = state["seq"]
            self._touched = state["touched"]
            self.cache = {}
            return
        self.nodes = {
            ipaddress.ip_network(p): {
                "parent": ipaddress.ip_network(rec["parent"]) if rec["parent"] else None,
                "root": ipaddress.ip_network(rec["root"]),
                "quota": rec["quota"],
                "base_yes": rec["base_yes"],
                "base_no": rec["base_no"],
                "adaptive_yes": rec["adaptive_yes"],
                "adaptive_no": rec["adaptive_no"],
                "closed": rec["closed"],
                "heap_score": None,
            }
            for p, rec in state["nodes"].items()
        }
        self.sent = state["sent"]
        self.positive_count = state["positive_count"]
        self.root_index = state["root_index"]
        self.epoch = state["epoch"]
        self.cache = {}
        self.heap = [
            (score, seq, ipaddress.ip_network(node))
            for score, seq, node in state.get("heap", [])
        ]
        self.seq = state.get("seq", 0)
        self._touched = {ipaddress.ip_network(node) for node in state.get("touched", [])}
        if "heap" in state:
            for score, _, node in self.heap:
                self.nodes[node]["heap_score"] = -score
            heapq.heapify(self.heap)
            return
        for node, rec in self.nodes.items():
            if rec["closed"]:
                continue
            if node.prefixlen == 64 and self._node_sent(rec) >= rec["quota"]:
                continue
            self._heap_push(node)
