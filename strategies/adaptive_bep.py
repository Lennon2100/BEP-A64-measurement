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

State is O(screened nodes); cross-level dedup is cursor-based (see Targets).  A
"search layer" is one frontier scheduling round over nodes of possibly
different absolute lengths.
"""

import heapq
import ipaddress
import math
from dataclasses import dataclass

from strategies.common import Frame


def _entropy(q):
    return 0.0 if q in (0.0, 1.0) else -q * math.log(q) - (1.0 - q) * math.log(1.0 - q)


def load_frame(cfg, base):
    inputs = cfg["input"]
    prefix_key = (
        "adaptive_bep_prefix_csv"
        if "adaptive_bep_prefix_csv" in inputs
        else "journal_prefix_csv"
    )
    return Frame.from_bgp_prefixes(
        base / inputs[prefix_key], base / inputs["frame_exclusions"]
    )


@dataclass(slots=True)
class Node:
    parent: ipaddress.IPv6Network | None
    root: ipaddress.IPv6Network
    quota: int
    prior_alpha: float
    prior_beta: float
    base_yes: int = 0
    base_no: int = 0
    adaptive_yes: int = 0
    adaptive_no: int = 0
    closed: bool = False
    blocked: bool = False
    bgp_signal: float = 0.0
    nearest_bgp_length: int | None = None


class Strategy:
    name = "adaptive_bep"

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
        if not (
            self.theta > 0
            and self.prior_strength > 0
            and 0 < self.prior_decay <= 1
            and self.info_weight >= 0
            and self.bgp_weight >= 0
            and 0 < self.root_fraction < 1
            and self.steps
            and all(1 <= step <= 8 for step in self.steps)
        ):
            raise ValueError("invalid adaptive BEP search coefficients or step_bits")

        # Base and adaptive samples are kept separately.  Both are uniform in
        # this node and therefore both are valid for its local likelihood.
        self.nodes = {}
        self.heap = []           # (-score, seq, prefix) max heap
        self.seq = 0
        self.sent = 0
        self.positive_count = 0
        self.root_order = sorted(frame.roots, key=lambda p: (-frame.descendants[p], str(p)))
        self.root_index = 0
        self.root_phase_budget = allowance * self.root_fraction
        root_base_total = sum(self.base(root) for root in self.root_order)
        if root_base_total > self.root_phase_budget:
            if root_base_total > allowance:
                raise ValueError(
                    f"adaptive BEP root bases require {root_base_total} probes, "
                    f"exceeding allowance {allowance}"
                )
            required_fraction = root_base_total / allowance
            raise ValueError(
                f"adaptive BEP root bases require {root_base_total} probes; "
                f"root_budget_fraction must be at least {required_fraction:.6f}"
            )

    # ---- quota and posterior -------------------------------------------------

    def base(self, prefix):
        if prefix.prefixlen == 64:
            return 1
        count = 1 << (64 - prefix.prefixlen)
        return min(count, max(1, math.ceil(self.theta * 2 ** ((56 - prefix.prefixlen) / 4))))

    def _posterior(self, prefix):
        rec = self.nodes[prefix]
        return (
            rec.base_yes + rec.adaptive_yes + rec.prior_alpha,
            rec.base_no + rec.adaptive_no + rec.prior_beta,
        )

    def _score(self, prefix):
        a, b = self._posterior(prefix)
        p = a / (a + b)
        information = _entropy(p) - p * _entropy((a + 1) / (a + b + 1)) - (1 - p) * _entropy(a / (a + b + 1))
        bgp_bonus = self.bgp_weight * self.nodes[prefix].bgp_signal
        return p + self.info_weight * information + bgp_bonus

    # ---- activation and child selection --------------------------------------

    def _bgp_signal(self, root, count, deepest):
        if not count:
            return 0.0
        root_count = len(self.frame.prefixes_by_root[root])
        density = math.log1p(count) / math.log1p(root_count)
        return (deepest / 64 + density) / 2

    def _node_bgp_stats(self, prefix, root):
        if prefix in self.frame.prefix_set:
            children = self.frame.children.get(prefix, ())
            return (
                self.frame.descendants[prefix] + 1,
                self.frame.deepest[prefix],
                min((child.prefixlen for child in children), default=None),
            )
        contained = [
            candidate
            for candidate in self.frame.prefixes_by_root[root]
            if candidate.subnet_of(prefix)
        ]
        return (
            len(contained),
            max((candidate.prefixlen for candidate in contained), default=0),
            min(
                (candidate.prefixlen for candidate in contained if candidate.prefixlen > prefix.prefixlen),
                default=None,
            ),
        )

    def _activate(self, prefix, parent, blocked=False, bgp_stats=None):
        ancestors = []
        ancestor = parent
        while ancestor is not None:
            ancestors.append(ancestor)
            ancestor = self.nodes[ancestor].parent
        quota = max(0, self.base(prefix) - self.targets.prior_count(prefix, ancestors))
        root = prefix if parent is None else self.nodes[parent].root
        if bgp_stats is None:
            bgp_stats = self._node_bgp_stats(prefix, root)
        bgp_count, bgp_deepest, nearest_bgp_length = bgp_stats
        if parent is None:
            prior_alpha = prior_beta = self.prior_strength / 2
        else:
            # Splitting closes the parent, so this inherited prior is final.
            pa, pb = self._posterior(parent)
            mean = pa / (pa + pb)
            strength = max(
                0.02,
                self.prior_strength
                * self.prior_decay ** (prefix.prefixlen - parent.prefixlen),
            )
            prior_alpha, prior_beta = mean * strength, (1 - mean) * strength
        self.nodes[prefix] = Node(
            parent=parent,
            root=root,
            quota=quota,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            blocked=blocked,
            bgp_signal=self._bgp_signal(root, bgp_count, bgp_deepest),
            nearest_bgp_length=nearest_bgp_length,
        )

    def _choose_step(self, prefix):
        rec = self.nodes[prefix]
        nearest = rec.nearest_bgp_length
        gap = nearest - prefix.prefixlen if nearest is not None else 1
        valid = [step for step in self.steps if prefix.prefixlen + step <= 64]
        return max((step for step in valid if step <= gap), default=min(valid, default=None))

    def _child_bgp_stats(self, prefix, step):
        """Avoid rescanning the same BGP subtree separately for every sibling."""
        child_length = prefix.prefixlen + step
        child_count = 1 << step
        child_size = 1 << (128 - child_length)
        start = int(prefix.network_address)
        child_stats = [[0, 0, None] for _ in range(child_count)]
        root = self.nodes[prefix].root
        for candidate in self.frame.prefixes_by_root[root]:
            if candidate.prefixlen < child_length or not candidate.subnet_of(prefix):
                continue
            index = (int(candidate.network_address) - start) // child_size
            stats = child_stats[index]
            stats[0] += 1
            stats[1] = max(stats[1], candidate.prefixlen)
            if candidate.prefixlen > child_length:
                stats[2] = (
                    candidate.prefixlen
                    if stats[2] is None
                    else min(stats[2], candidate.prefixlen)
                )
        return child_stats

    @staticmethod
    def _child_at(prefix, step, index):
        length = prefix.prefixlen + step
        size = 1 << (128 - length)
        address = int(prefix.network_address) + index * size
        return ipaddress.ip_network((address, length))

    # ---- frontier heap --------------------------------------------------------

    def _heap_push(self, prefix):
        # A queued node changes score only after it is popped, probed, and reinserted.
        score = self._score(prefix)
        heapq.heappush(self.heap, (-score, self.seq, prefix))
        self.seq += 1

    def _heap_pop(self):
        while self.heap:
            _, _, prefix = heapq.heappop(self.heap)
            rec = self.nodes.get(prefix)
            if rec is None or rec.closed or rec.blocked:
                continue
            if prefix.prefixlen == 64 and self._node_sent(rec) >= rec.quota:
                rec.closed = True
                continue
            return prefix
        return None

    # ---- planning and streaming ----------------------------------------------

    def _iter_plan(self, limit):
        remaining = limit
        planned = self.sent
        while remaining > 0 and self.root_index < len(self.root_order) and planned < self.root_phase_budget:
            root = self.root_order[self.root_index]
            self.root_index += 1
            quota = self.base(root)
            if quota > self.allowance - planned:
                raise RuntimeError("adaptive BEP root-base preflight invariant failed")
            self._activate(root, None)
            count = min(remaining, quota)
            planned += count
            remaining -= count
            yield root, count, "base"
        while remaining > 0:
            node = self._heap_pop()
            if node is None:
                break
            rec = self.nodes[node]
            sent = self._node_sent(rec)
            if sent < rec.quota:
                count = min(remaining, rec.quota - sent)
                mode = "base"
            else:
                step = self._choose_step(node)
                if step is None:
                    rec.closed = True
                    continue
                child_count = 1 << step
                if remaining < child_count:
                    self._heap_push(node)
                    break
                child_bgp_stats = self._child_bgp_stats(node, step)
                for index in range(child_count):
                    child = self._child_at(node, step, index)
                    if child in self.nodes:
                        raise RuntimeError(f"adaptive BEP split produced existing child {child}")
                    self._activate(
                        child,
                        node,
                        bgp_stats=child_bgp_stats[index],
                    )
                    mode = "base" if self.nodes[child].quota else "dynamic"
                    remaining -= 1
                    yield child, 1, mode
                rec.closed = True
                continue
            remaining -= count
            yield node, count, mode

    def iter_targets(self, limit):
        for node, count, mode in self._iter_plan(limit):
            ancestors = []
            parent = self.nodes[node].parent
            while parent is not None:
                ancestors.append(parent)
                parent = self.nodes[parent].parent
            for _ in range(count):
                c64 = self.targets.draw(node, ancestors)
                if c64 is None:
                    self.nodes[node].closed = True
                    break
                yield (c64, str(node), mode)

    # ---- feedback and persistence --------------------------------------------

    @staticmethod
    def _node_sent(rec):
        return rec.base_yes + rec.base_no + rec.adaptive_yes + rec.adaptive_no

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        node = ipaddress.ip_network(node_str)
        stem = "base" if mode == "base" else "adaptive"
        rec = self.nodes[node]
        setattr(rec, f"{stem}_yes", getattr(rec, f"{stem}_yes") + positives)
        setattr(rec, f"{stem}_no", getattr(rec, f"{stem}_no") + probes - positives)
        self.sent += probes
        self.positive_count += positives
        rec.blocked = False
        if rec.closed or (node.prefixlen == 64 and self._node_sent(rec) >= rec.quota):
            if node.prefixlen == 64:
                del self.nodes[node]
                self.targets.forget(node)
            return
        self._heap_push(node)

    def finish_batch(self):
        pass

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

    def rebuild_after_recovery(self):
        self.heap = []
        self.seq = 0
        for node, rec in self.nodes.items():
            if rec.closed or rec.blocked:
                continue
            if node.prefixlen == 64 and self._node_sent(rec) >= rec.quota:
                continue
            self._heap_push(node)

    def snapshot(self):
        return {
            "nodes": {
                str(p): {
                    "parent": str(rec.parent) if rec.parent else None,
                    "quota": rec.quota,
                    "root": str(rec.root),
                    "prior_alpha": rec.prior_alpha,
                    "prior_beta": rec.prior_beta,
                    "base_yes": rec.base_yes,
                    "base_no": rec.base_no,
                    "adaptive_yes": rec.adaptive_yes,
                    "adaptive_no": rec.adaptive_no,
                    "closed": rec.closed,
                    "blocked": rec.blocked,
                    "bgp_signal": rec.bgp_signal,
                    "nearest_bgp_length": rec.nearest_bgp_length,
                }
                for p, rec in self.nodes.items()
            },
            "sent": self.sent,
            "positive_count": self.positive_count,
            "root_index": self.root_index,
            "heap": [[score, seq, str(node)] for score, seq, node in self.heap],
            "seq": self.seq,
        }

    def checkpoint(self):
        """Return live node state for bounded-overhead checkpoint serialization."""
        return {
            "native": True,
            "nodes": self.nodes,
            "sent": self.sent,
            "positive_count": self.positive_count,
            "root_index": self.root_index,
            "heap": self.heap,
            "seq": self.seq,
        }

    def restore(self, state):
        if state.get("native"):
            old_nodes = state["nodes"]
            self.nodes = self._restore_nodes(old_nodes)
            self.sent = state["sent"]
            self.positive_count = state["positive_count"]
            self.root_index = state["root_index"]
            self.heap = state["heap"]
            self.seq = state["seq"]
            return
        old_nodes = {
            ipaddress.ip_network(prefix): rec for prefix, rec in state["nodes"].items()
        }
        self.nodes = self._restore_nodes(old_nodes)
        self.sent = state["sent"]
        self.positive_count = state["positive_count"]
        self.root_index = state["root_index"]
        self.heap = [
            (score, seq, ipaddress.ip_network(node))
            for score, seq, node in state.get("heap", [])
        ]
        self.seq = state.get("seq", 0)
        if "heap" in state:
            heapq.heapify(self.heap)
            return
        for node, rec in self.nodes.items():
            if rec.closed:
                continue
            if node.prefixlen == 64 and self._node_sent(rec) >= rec.quota:
                continue
            self._heap_push(node)

    def _restore_nodes(self, saved):
        if not saved or isinstance(next(iter(saved.values())), Node):
            return saved
        for prefix in sorted(saved, key=lambda item: item.prefixlen):
            rec = saved[prefix]
            parent = rec.get("parent")
            if isinstance(parent, str):
                parent = ipaddress.ip_network(parent)
            if "prior_alpha" in rec:
                prior_alpha, prior_beta = rec["prior_alpha"], rec["prior_beta"]
            elif parent is None:
                prior_alpha = prior_beta = self.prior_strength / 2
            else:
                pa, pb = self._posterior_from(saved[parent])
                mean = pa / (pa + pb)
                strength = max(
                    0.02,
                    self.prior_strength
                    * self.prior_decay ** (prefix.prefixlen - parent.prefixlen),
                )
                prior_alpha, prior_beta = mean * strength, (1 - mean) * strength
            root = rec.get("root", prefix if parent is None else saved[parent].root)
            if isinstance(root, str):
                root = ipaddress.ip_network(root)
            saved[prefix] = Node(
                parent=parent,
                root=root,
                quota=rec["quota"],
                prior_alpha=prior_alpha,
                prior_beta=prior_beta,
                base_yes=rec["base_yes"],
                base_no=rec["base_no"],
                adaptive_yes=rec["adaptive_yes"],
                adaptive_no=rec["adaptive_no"],
                closed=rec["closed"],
                blocked=rec.get("blocked", False),
                bgp_signal=rec.get("bgp_signal", 0.0),
                nearest_bgp_length=rec.get("nearest_bgp_length"),
            )
        return saved

    @staticmethod
    def _posterior_from(rec):
        return (
            rec.base_yes + rec.adaptive_yes + rec.prior_alpha,
            rec.base_no + rec.adaptive_no + rec.prior_beta,
        )
