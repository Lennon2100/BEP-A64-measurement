"""BGP-first bounded-memory test strategy for the legacy journal method.

Phase one exhaustively probes the union of announced BGP prefixes from /48
through /64, visiting more-specific announcements first. Overlapping
announcements are partitioned into disjoint /64 intervals, so every physical
probe is charged once.

Phase two partitions the remaining space announced by prefixes shorter than
/48 into lazy /48 ranges. A global heap contains one record per range rather
than one record per generated tree node. The best range contributes one /48;
all /64s in that /48 not already covered in phase one are then probed.
"""

import bisect
import heapq
import ipaddress
import math
from array import array

from strategies.common import Frame


STATE_VERSION = 2
FULL_C48 = 1 << 16


def _entropy(q):
    return 0.0 if q in (0.0, 1.0) else -q * math.log(q) - (1.0 - q) * math.log(1.0 - q)


def _gaps(start, stop, exclusions):
    cursor = start
    for excluded_start, excluded_stop in sorted(exclusions):
        if excluded_stop <= cursor:
            continue
        if excluded_start > cursor:
            yield cursor, min(excluded_start, stop)
        cursor = max(cursor, excluded_stop)
        if cursor >= stop:
            return
    if cursor < stop:
        yield cursor, stop


def load_frame(cfg, base):
    inputs = cfg["input"]
    return Frame.from_bgp_prefixes(
        base / inputs["journal_prefix_csv"], base / inputs["frame_exclusions"]
    )


class Strategy:
    name = "journal"
    minimum_batch_size = FULL_C48

    def __init__(self, frame, targets, cfg, allowance):
        self.frame = frame
        self.targets = targets
        self.allowance = allowance
        self.prior_strength = float(cfg["prior_strength"])
        self.info_weight = float(cfg["information_weight"])
        self.bgp_weight = float(cfg["bgp_weight"])
        if self.prior_strength <= 0 or self.info_weight < 0 or self.bgp_weight < 0:
            raise ValueError("invalid journal search coefficients")

        self.cover_segments = self._cover_segments()
        self.deep_intervals, self.deep_starts = self._deep_union()
        self.cover_index = 0
        self.cover_cursor = self.cover_segments[0][1] if self.cover_segments else 0

        self.arms = self._gap_arms()
        self.arm_next = array("Q", (start for _, start, _ in self.arms))
        self.arm_yes = array("Q", [0]) * len(self.arms)
        self.arm_no = array("Q", [0]) * len(self.arms)
        self.heap = []
        self.seq = 0
        for index in range(len(self.arms)):
            self._push_arm(index)

        self.pending = {}
        self.touched_arms = set()
        self.sent = 0
        self.positive_count = 0

    @staticmethod
    def _c64_bounds(prefix):
        start = int(prefix.network_address) >> 64
        return start, start + (1 << (64 - prefix.prefixlen))

    @staticmethod
    def _c48_bounds(prefix):
        start = int(prefix.network_address) >> 80
        return start, start + (1 << (48 - prefix.prefixlen))

    def _cover_segments(self):
        segments = []
        for prefix in self.frame.prefixes:
            if prefix.prefixlen < 48:
                continue
            start, stop = self._c64_bounds(prefix)
            exclusions = [
                self._c64_bounds(child)
                for child in self.frame.children.get(prefix, ())
            ]
            for gap_start, gap_stop in _gaps(start, stop, exclusions):
                segments.append((prefix, gap_start, gap_stop))
        segments.sort(key=lambda item: (-item[0].prefixlen, item[1]))
        return segments

    def _deep_union(self):
        intervals = sorted((start, stop) for _, start, stop in self.cover_segments)
        merged = []
        for start, stop in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
            else:
                merged.append((start, stop))
        return merged, [start for start, _ in merged]

    def _gap_arms(self):
        arms = []
        for prefix in self.frame.prefixes:
            if prefix.prefixlen >= 48:
                continue
            start, stop = self._c48_bounds(prefix)
            exclusions = [
                self._c48_bounds(child)
                for child in self.frame.children.get(prefix, ())
                if child.prefixlen < 48
            ]
            for gap_start, gap_stop in _gaps(start, stop, exclusions):
                arms.append((prefix, gap_start, gap_stop))
        arms.sort(key=lambda item: (-item[0].prefixlen, item[1]))
        return arms

    def _arm_score(self, index):
        alpha = self.prior_strength / 2 + self.arm_yes[index]
        beta = self.prior_strength / 2 + self.arm_no[index]
        probability = alpha / (alpha + beta)
        information = (
            _entropy(probability)
            - probability * _entropy((alpha + 1) / (alpha + beta + 1))
            - (1 - probability) * _entropy(alpha / (alpha + beta + 1))
        )
        owner = self.arms[index][0]
        bgp_signal = (
            owner.prefixlen / 48
            + math.log1p(self.frame.descendants[owner] + 1)
            / math.log1p(len(self.frame.prefixes) + 1)
        ) / 2
        return probability + self.info_weight * information + self.bgp_weight * bgp_signal

    def _push_arm(self, index):
        if self.arm_next[index] >= self.arms[index][2]:
            return
        heapq.heappush(self.heap, (-self._arm_score(index), self.seq, index))
        self.seq += 1

    def _pop_arm(self):
        while self.heap:
            _, _, index = heapq.heappop(self.heap)
            if self.arm_next[index] < self.arms[index][2]:
                return index
        return None

    def _covered_by_deep_bgp(self, c64):
        position = bisect.bisect_right(self.deep_starts, c64) - 1
        return position >= 0 and c64 < self.deep_intervals[position][1]

    def _next_cover(self):
        while self.cover_index < len(self.cover_segments):
            owner, start, stop = self.cover_segments[self.cover_index]
            if self.cover_cursor < start:
                self.cover_cursor = start
            if self.cover_cursor < stop:
                c64 = self.cover_cursor
                self.cover_cursor += 1
                return c64, owner
            self.cover_index += 1
            if self.cover_index < len(self.cover_segments):
                self.cover_cursor = self.cover_segments[self.cover_index][1]
        return None

    @staticmethod
    def _c48_network(c48):
        return ipaddress.ip_network((c48 << 80, 48))

    def iter_targets(self, limit):
        emitted = 0
        while emitted < limit:
            target = self._next_cover()
            if target is None:
                break
            c64, owner = target
            emitted += 1
            yield c64, str(owner), "bgp_cover"
        if emitted or self.cover_index < len(self.cover_segments):
            return

        while limit - emitted >= FULL_C48:
            arm = self._pop_arm()
            if arm is None:
                return
            c48 = self.arm_next[arm]
            self.arm_next[arm] += 1
            node = self._c48_network(c48)
            self.pending[str(node)] = arm
            start = c48 << 16
            action_probes = 0
            for c64 in range(start, start + FULL_C48):
                if self._covered_by_deep_bgp(c64):
                    continue
                action_probes += 1
                emitted += 1
                yield c64, str(node), "bgp_gap_cover"
            if action_probes == 0:
                self.pending.pop(str(node), None)
                self._push_arm(arm)

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        self.sent += probes
        self.positive_count += positives
        if mode != "bgp_gap_cover":
            return
        if node_str not in self.pending:
            raise ValueError(f"missing journal /48 action state for {node_str}")
        arm = self.pending.pop(node_str)
        self.arm_yes[arm] += positives
        self.arm_no[arm] += probes - positives
        self.touched_arms.add(arm)

    def finish_batch(self):
        for arm in self.touched_arms:
            self._push_arm(arm)
        self.touched_arms.clear()

    def adopt_manifest_actions(self, actions):
        raise ValueError(
            "this journal strategy requires prepared-state.pkl.gz for scanned-batch recovery"
        )

    def rebuild_after_recovery(self):
        return

    def snapshot(self):
        return {
            "journal_state_version": STATE_VERSION,
            "cover_index": self.cover_index,
            "cover_cursor": self.cover_cursor,
            "arm_next": list(self.arm_next),
            "arm_yes": list(self.arm_yes),
            "arm_no": list(self.arm_no),
            "heap": [list(entry) for entry in self.heap],
            "seq": self.seq,
            "pending": dict(self.pending),
            "touched_arms": sorted(self.touched_arms),
            "sent": self.sent,
            "positive_count": self.positive_count,
        }

    def checkpoint(self):
        return {
            "journal_state_version": STATE_VERSION,
            "cover_index": self.cover_index,
            "cover_cursor": self.cover_cursor,
            "arm_next": self.arm_next,
            "arm_yes": self.arm_yes,
            "arm_no": self.arm_no,
            "heap": self.heap,
            "seq": self.seq,
            "pending": self.pending,
            "touched_arms": self.touched_arms,
            "sent": self.sent,
            "positive_count": self.positive_count,
        }

    def restore(self, state):
        if state.get("journal_state_version") != STATE_VERSION:
            raise ValueError(
                "journal strategy changed; start a new run with a new output_root"
            )
        arm_next = array("Q", state["arm_next"])
        arm_yes = array("Q", state["arm_yes"])
        arm_no = array("Q", state["arm_no"])
        if not len(self.arms) == len(arm_next) == len(arm_yes) == len(arm_no):
            raise ValueError("journal checkpoint does not match the current BGP frame")
        self.cover_index = state["cover_index"]
        self.cover_cursor = state["cover_cursor"]
        self.arm_next = arm_next
        self.arm_yes = arm_yes
        self.arm_no = arm_no
        self.heap = [tuple(entry) for entry in state["heap"]]
        heapq.heapify(self.heap)
        self.seq = state["seq"]
        self.pending = dict(state.get("pending", {}))
        self.touched_arms = set(state.get("touched_arms", ()))
        self.sent = state["sent"]
        self.positive_count = state["positive_count"]
