"""Task-adapted reproduction of the fixed-level IWQoS BEP strategy.

The conference method traverses /32, /40, /48, /56, and /64 levels, probes
each active node according to Table I, and expands all 256 children only when
the node's 95% posterior lower bound exceeds its depth-dependent threshold.
Its native likelihood counts slow AU as positive, fast AU/TX/RR as negative,
and excludes null observations.  Common IMC discoveries are still accounted
by the formal runner for equal-method comparison.
"""

import bisect
import ipaddress
import math
from array import array

from scripts.prepare_campaign import load_exclusions, load_rows


LEVELS = (32, 40, 48, 56, 64)
PROBE_TABLE = (1024, 256, 64, 16, 1)


class ConferenceFrame:
    """BGP-routed space normalized to the paper's unique /32 roots."""

    def __init__(self, prefixes):
        intervals = []
        for prefix in prefixes:
            first = int(prefix.network_address) >> 96
            count = 1 << max(0, 32 - prefix.prefixlen)
            intervals.append((first, first + count))
        intervals.sort()
        self.intervals = self._merge(intervals)
        self.ends = array("Q")
        total = 0
        for start, stop in self.intervals:
            total += stop - start
            self.ends.append(total)
        self.root_count = total

    @staticmethod
    def _merge(intervals):
        merged = []
        for start, stop in intervals:
            if merged and start <= merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (previous[0], max(previous[1], stop))
            else:
                merged.append((start, stop))
        return merged

    def root_at(self, rank):
        interval = bisect.bisect_right(self.ends, rank)
        previous = self.ends[interval - 1] if interval else 0
        return self.intervals[interval][0] + rank - previous


def load_frame(cfg, base):
    inputs = cfg["input"]
    rows, _ = load_rows(
        base / inputs["bep_conference_prefix_csv"],
        load_exclusions(base / inputs["frame_exclusions"]),
    )
    return ConferenceFrame(rows)


class Strategy:
    name = "bep_conference"
    minimum_batch_size = max(PROBE_TABLE)

    def __init__(self, frame, targets, cfg, allowance):
        self.frame = frame
        self.targets = targets
        self.allowance = allowance
        self._load_config(cfg)
        self._initialize_state()

    def _load_config(self, cfg):
        self.prior_decay = float(cfg.get("prior_decay", 0.5))
        self.minimum_strength = float(cfg.get("minimum_strength", 2.0))
        self.credible_z = float(cfg.get("credible_z", 1.96))
        self.expansion_cost = float(cfg.get("expansion_cost", 1.0))
        if not (
            0 < self.prior_decay < 1
            and self.minimum_strength > 0
            and self.credible_z > 0
            and self.expansion_cost > 0
        ):
            raise ValueError("invalid BEP conference coefficients")

    def _initialize_state(self):
        self.sent = 0
        self.positive_count = 0
        self.native_positive_count = 0
        self.level_index = 0
        self.root_cursor = 0
        self.parent_cursor = 0
        self.child_cursor = 0
        self.level_exhausted = False
        self.done = False
        self.permutation_seeds = [
            (self.targets.rng.getrandbits(64), self.targets.rng.getrandbits(64))
            for _ in LEVELS
        ]
        self.source_prefixes = array("Q")
        self.source_alpha = array("d")
        self.source_beta = array("d")
        self.source_cursors = array("I")
        self.next_prefixes = array("Q")
        self.next_alpha = array("d")
        self.next_beta = array("d")
        self.next_cursors = array("I")
        self.pending = {}
        self.terminal_pending = 0
        self.ready_node = None

    @property
    def level(self):
        return LEVELS[self.level_index]

    def _child_prior(self, alpha, beta):
        mean = alpha / (alpha + beta)
        strength = max((alpha + beta) * self.prior_decay, self.minimum_strength)
        return mean * strength + 1.0, (1.0 - mean) * strength + 1.0

    def _next_node(self):
        if self.ready_node is not None:
            node = self.ready_node
            self.ready_node = None
            return node
        if self.level_index == 0:
            return self._next_root()
        return self._next_child()

    def _next_root(self):
        if self.root_cursor >= self.frame.root_count:
            return None
        root = self.frame.root_at(self.root_cursor)
        self.root_cursor += 1
        return root << 32, 1.0, 1.0, ()

    def _next_child(self):
        if self.parent_cursor >= len(self.source_prefixes):
            return None
        parent = self.source_prefixes[self.parent_cursor]
        shift = 64 - self.level
        prefix = parent | (self.child_cursor << shift)
        alpha, beta = self._child_prior(
            self.source_alpha[self.parent_cursor], self.source_beta[self.parent_cursor]
        )
        start = self.parent_cursor * self.level_index
        path = tuple(self.source_cursors[start:start + self.level_index])
        self.child_cursor += 1
        if self.child_cursor == 256:
            self.child_cursor = 0
            self.parent_cursor += 1
        return prefix, alpha, beta, path

    def _has_more_nodes(self):
        if self.ready_node is not None:
            return True
        if self.level_index == 0:
            return self.root_cursor < self.frame.root_count
        return self.parent_cursor < len(self.source_prefixes)

    def _permutation(self, prefix, level):
        count = 1 << (64 - level)
        if count == 1:
            return 0, 1, count
        seed_offset, seed_stride = self.permutation_seeds[LEVELS.index(level)]
        mask = count - 1
        offset = (prefix ^ seed_offset) & mask
        stride = ((prefix >> 17) ^ seed_stride) & mask
        return offset, stride | 1, count

    def _ancestor_ranges(self, prefix, path):
        ranges = []
        for index, cursor in enumerate(path):
            level = LEVELS[index]
            count = 1 << (64 - level)
            ancestor = prefix & ~(count - 1)
            offset, stride, _ = self._permutation(ancestor, level)
            ranges.append((ancestor, count, offset, pow(stride, -1, count), cursor))
        return ranges

    @staticmethod
    def _was_sent(c64, ancestor_range):
        ancestor, count, offset, inverse, cursor = ancestor_range
        index = ((c64 - ancestor - offset) * inverse) % count
        return index < cursor

    def _node_targets(self, prefix, count, path):
        offset, stride, capacity = self._permutation(prefix, self.level)
        ancestor_ranges = self._ancestor_ranges(prefix, path)
        c64_values = []
        cursor = 0
        while cursor < capacity and len(c64_values) < count:
            c64 = prefix + ((offset + stride * cursor) % capacity)
            cursor += 1
            duplicate = any(self._was_sent(c64, prior) for prior in ancestor_ranges)
            if not duplicate:
                c64_values.append(c64)
        return c64_values, cursor

    def _node_name(self, prefix):
        return str(ipaddress.ip_network((prefix << 64, self.level)))

    def _new_pending(self, prefix, alpha, beta, path, raw_cursor, probes):
        node = self._node_name(prefix)
        self.pending[node] = {
            "prefix": prefix,
            "alpha": alpha,
            "beta": beta,
            "path": path,
            "raw_cursor": raw_cursor,
            "probes": probes,
            "bep_active": 0,
            "bep_inactive": 0,
        }
        return node

    def _prepare_node(self, prefix, alpha, beta, path, requested):
        c64_values, raw_cursor = self._node_targets(prefix, requested, path)
        if not c64_values:
            return None, ()
        if self.level == 64:
            self.terminal_pending += len(c64_values)
            return "conference-terminal", c64_values
        node = self._new_pending(
            prefix, alpha, beta, path, raw_cursor, len(c64_values)
        )
        return node, c64_values

    def iter_targets(self, limit):
        if self.done:
            return
        if self.pending or self.terminal_pending:
            raise RuntimeError("BEP conference feedback is pending")
        produced = 0
        while produced < limit and self.sent + produced < self.allowance:
            prefix, alpha, beta, path = self._next_node() or (None, None, None, None)
            if prefix is None:
                self.level_exhausted = True
                break
            remaining = self.allowance - self.sent - produced
            requested = min(PROBE_TABLE[self.level_index], remaining)
            if requested > limit - produced and produced:
                self.ready_node = (prefix, alpha, beta, path)
                break
            requested = min(requested, limit - produced)
            node, c64_values = self._prepare_node(
                prefix, alpha, beta, path, requested
            )
            if node is None:
                continue
            mode = f"conference_{self.level}"
            for c64 in c64_values:
                yield c64, node, mode
                produced += 1
            if not self._has_more_nodes():
                self.level_exhausted = True

    def feed_class_aggregate(self, row):
        if self.level == 64 and row["node"] == "conference-terminal":
            if self._class_count(row) != int(row["probes"]):
                raise ValueError("BEP conference response classes do not cover terminal probes")
            self.native_positive_count += int(row["bep_active"])
            return
        rec = self.pending[row["node"]]
        active = int(row["bep_active"])
        inactive = int(row["bep_inactive"])
        null = int(row["bep_null"])
        if active + inactive + null != int(row["probes"]):
            raise ValueError("BEP conference response classes do not cover the node probes")
        rec["bep_active"] = active
        rec["bep_inactive"] = inactive
        self.native_positive_count += active

    @staticmethod
    def _class_count(row):
        return sum(int(row[key]) for key in ("bep_active", "bep_inactive", "bep_null"))

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        if self.level == 64 and node_str == "conference-terminal":
            if mode != "conference_64" or probes != self.terminal_pending:
                raise ValueError("unexpected BEP conference terminal feedback")
            self.sent += probes
            self.positive_count += positives
            return
        if node_str not in self.pending or mode != f"conference_{self.level}":
            raise ValueError("unexpected BEP conference feedback")
        if probes != self.pending[node_str]["probes"]:
            raise ValueError("BEP conference feedback probe count mismatch")
        self.sent += probes
        self.positive_count += positives

    def _credible_lower(self, alpha, beta):
        mean = alpha / (alpha + beta)
        variance = mean * (1.0 - mean) / (alpha + beta + 1.0)
        return max(0.0, mean - self.credible_z * math.sqrt(variance))

    def _expand(self, rec):
        alpha = rec["alpha"] + rec["bep_active"]
        beta = rec["beta"] + rec["bep_inactive"]
        threshold = self.expansion_cost / (1 << (64 - self.level))
        if self.level == 64 or self._credible_lower(alpha, beta) <= threshold:
            return
        self.next_prefixes.append(rec["prefix"])
        self.next_alpha.append(alpha)
        self.next_beta.append(beta)
        self.next_cursors.extend((*rec["path"], rec["raw_cursor"]))

    def _advance_level(self):
        if self.level == 64 or not self.next_prefixes:
            self.done = True
            return
        self.source_prefixes = self.next_prefixes
        self.source_alpha = self.next_alpha
        self.source_beta = self.next_beta
        self.source_cursors = self.next_cursors
        self.next_prefixes = array("Q")
        self.next_alpha = array("d")
        self.next_beta = array("d")
        self.next_cursors = array("I")
        self.level_index += 1
        self.parent_cursor = 0
        self.child_cursor = 0
        self.level_exhausted = False

    def finish_batch(self):
        for rec in self.pending.values():
            self._expand(rec)
        self.pending.clear()
        self.terminal_pending = 0
        if self.level_exhausted:
            self._advance_level()

    def checkpoint(self):
        state = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"frame", "targets", "allowance"}
        }
        return {"native": True, **state}

    def snapshot(self):
        return self.checkpoint()

    def restore(self, state):
        if not state.get("native"):
            raise ValueError("BEP conference has no legacy checkpoint format")
        for key, value in state.items():
            if key != "native" and key not in {"frame", "targets", "allowance"}:
                setattr(self, key, value)
