"""Budget-charged, BGP-only adaptation of the TNet active-network search.

The paper first screens every routed /48 with one random /64 target, keeps
regions whose last-hop Address-Unreachable source is unique, then performs ten
feedback rounds with /48 Top-K/Boltzmann allocation and /52-level sampling.
The full screen is larger than this campaign's budget, so this module samples
the routed /48 union uniformly without replacement up to the explicit charged
screen budget.  No external candidate list is used.
"""

import bisect
import heapq
import ipaddress
import math
from array import array

from scripts.prefix_data import load_exclusions, load_rows


class TNetFrame:
    """Merged routed /48 intervals, addressed by a dense integer rank."""

    def __init__(self, prefixes):
        intervals = []
        for prefix in prefixes:
            first = int(prefix.network_address) >> 80
            count = 1 << max(0, 48 - prefix.prefixlen)
            intervals.append((first, first + count))
        intervals.sort()

        self.intervals = []
        for start, stop in intervals:
            if self.intervals and start <= self.intervals[-1][1]:
                previous = self.intervals[-1]
                self.intervals[-1] = (previous[0], max(previous[1], stop))
            else:
                self.intervals.append((start, stop))

        self.ends = array("Q")
        total = 0
        for start, stop in self.intervals:
            total += stop - start
            self.ends.append(total)
        self.region_count = total

    def region_at(self, rank):
        index = bisect.bisect_right(self.ends, rank)
        previous = self.ends[index - 1] if index else 0
        return self.intervals[index][0] + rank - previous


def load_frame(cfg, base):
    inputs = cfg["input"]
    rows, _ = load_rows(
        base / inputs["tnet_prefix_csv"],
        load_exclusions(base / inputs["frame_exclusions"]),
    )
    return TNetFrame(rows)


class Strategy:
    name = "tnet"

    def __init__(self, frame, targets, cfg, allowance):
        self.frame = frame
        self.targets = targets
        self.allowance = allowance
        self._load_config(cfg)
        self._initialize_screen()
        self._initialize_search()

    def _load_config(self, cfg):
        requested_screen = int(cfg["candidate_screen_budget"])
        if not 0 < requested_screen < self.allowance:
            raise ValueError("TNet candidate_screen_budget must be between zero and the method budget")
        self.screen_budget = min(requested_screen, self.frame.region_count)
        self.rounds = int(cfg.get("rounds", 10))
        self.top_k_ratio = float(cfg.get("top_k_ratio", 0.04))
        self.temperature = float(cfg.get("initial_temperature", 1.0))
        if self.rounds <= 0:
            raise ValueError("TNet rounds must be positive")
        if not 0 < self.top_k_ratio <= 1:
            raise ValueError("TNet top_k_ratio must be in (0, 1]")
        if self.temperature <= 0:
            raise ValueError("TNet initial_temperature must be positive")

    def _initialize_screen(self):
        self.sent = 0
        self.positive_count = 0
        self.phase = "candidate_screen"
        self.screen_generated = 0
        self.screen_sent = 0
        self.screen_offset = self.targets.rng.randrange(self.frame.region_count)
        self.screen_stride = self._coprime_stride(self.frame.region_count)
        self.screen_slot_offset = self.targets.rng.randrange(1 << 16)
        self.screen_slot_stride = 2 * self.targets.rng.randrange(1 << 15) + 1
        self.region_router = {}
        self.router_region = {}

    def _initialize_search(self):
        self.prefixes = array("Q")
        self.screen_slots = array("H")
        self.region_probes = array("Q")
        self.region_hits = array("Q")
        self.region_draws = array("Q")
        self.round_probes = array("Q")
        self.round_hits = array("Q")
        self.sub_probes = array("H")
        self.sub_hits = array("H")
        self.sub_cursors = array("H")
        self.search_budget = self.allowance - self.screen_budget
        self.search_generated = 0
        self.round_index = 0
        self.round_generated = 0
        self.round_committed = 0
        self.selected = array("I")
        self.region_cdf = array("d")
        self.sub_cdf = array("d")
        self.uniform_offset = 0

    def _coprime_stride(self, count):
        if count == 1:
            return 1
        while True:
            stride = self.targets.rng.randrange(1, count)
            if math.gcd(stride, count) == 1:
                return stride

    def _screen_target(self):
        rank = (self.screen_offset + self.screen_stride * self.screen_generated) % self.frame.region_count
        prefix48 = self.frame.region_at(rank)
        slot = self._screen_slot(prefix48)
        self.screen_generated += 1
        return (prefix48 << 16) | slot

    def _screen_slot(self, prefix48):
        return (self.screen_slot_offset + self.screen_slot_stride * prefix48) & 0xFFFF

    def feed_router_observation(self, c64_text, router):
        """Collect last-hop uniqueness evidence during the charged screen."""
        if self.phase != "candidate_screen":
            return
        c64 = int(ipaddress.ip_network(c64_text, strict=True).network_address) >> 64
        region = c64 >> 16
        previous_router = self.region_router.get(region)
        if region not in self.region_router:
            self.region_router[region] = router
        elif previous_router != router:
            self.region_router[region] = None

        previous_region = self.router_region.get(router)
        if router not in self.router_region:
            self.router_region[router] = region
        elif previous_region != region:
            self.router_region[router] = None

    def _build_candidates(self):
        regions = sorted(
            region
            for region, router in self.region_router.items()
            if router is not None and self.router_region.get(router) == region
        )
        self._allocate_candidate_state(regions)
        self.region_router.clear()
        self.router_region.clear()
        self.phase = "search"
        if regions:
            self._prepare_round()

    def _allocate_candidate_state(self, regions):
        self.prefixes = array("Q", regions)
        self.screen_slots = array("H", (self._screen_slot(region) for region in regions))
        count = len(regions)
        self.region_probes = array("Q", [0]) * count
        self.region_hits = array("Q", [0]) * count
        self.region_draws = array("Q", [0]) * count
        self.round_probes = array("Q", [0]) * count
        self.round_hits = array("Q", [0]) * count
        slots = count * 16
        self.sub_probes = array("H", [0]) * slots
        self.sub_hits = array("H", [0]) * slots
        self.sub_cursors = array("H", [0]) * slots

    def _round_quota(self, index):
        base, extra = divmod(self.search_budget, self.rounds)
        return base + (index < extra)

    def _available(self, index):
        offset = index * 16
        used = sum(self.sub_cursors[offset + subnet] for subnet in range(16))
        return used < (1 << 16)

    def _prepare_round(self):
        if self.round_index >= self.rounds or not self.prefixes:
            self.selected = array("I")
            return
        available = (index for index in range(len(self.prefixes)) if self._available(index))
        if self.round_index == 0:
            self._prepare_uniform_round(available)
        else:
            self._prepare_adaptive_round(available)
        self.round_probes = array("Q", [0]) * len(self.prefixes)
        self.round_hits = array("Q", [0]) * len(self.prefixes)

    def _prepare_uniform_round(self, available):
        self.selected = array("I", available)
        self.region_cdf = array("d")
        self.sub_cdf = array("d")
        self.uniform_offset = self.targets.rng.randrange(len(self.selected)) if self.selected else 0

    def _round_score(self, index):
        probes = self.round_probes[index]
        return self.round_hits[index] / probes if probes else 0.0

    def _prepare_adaptive_round(self, available):
        keep = max(1, math.ceil(len(self.prefixes) * self.top_k_ratio))
        chosen = heapq.nlargest(
            keep, available, key=lambda index: (self._round_score(index), -index)
        )
        self.selected = array("I", chosen)
        temperature = self.temperature / (self.round_index + 1)
        self.region_cdf = self._region_weights(chosen, temperature)
        self.sub_cdf = self._subnet_weights(chosen, temperature)

    def _region_weights(self, chosen, temperature):
        scores = [self._round_score(index) for index in chosen]
        maximum = max(scores, default=0.0)
        cumulative = array("d")
        total = 0.0
        for score in scores:
            total += math.exp((score - maximum) / temperature)
            cumulative.append(total)
        return cumulative

    def _subnet_weights(self, chosen, temperature):
        cumulative = array("d")
        for index in chosen:
            rates = self._subnet_rates(index)
            maximum = max(rates)
            subtotal = 0.0
            for rate in rates:
                subtotal += math.exp((rate - maximum) / temperature)
                cumulative.append(subtotal)
        return cumulative

    def _subnet_rates(self, index):
        offset = index * 16
        return [
            self.sub_hits[offset + subnet] / self.sub_probes[offset + subnet]
            if self.sub_probes[offset + subnet] else 0.0
            for subnet in range(16)
        ]

    def _choose_region(self):
        if not self.selected:
            return None, None
        if self.round_index == 0:
            position = (self.uniform_offset + self.round_generated) % len(self.selected)
        else:
            draw = self.targets.rng.random() * self.region_cdf[-1]
            position = bisect.bisect_left(self.region_cdf, draw)
        return position, self.selected[position]

    def _choose_subnet(self, selection_position, region_index):
        if self.round_index == 0:
            start = self.region_draws[region_index] % 16
        else:
            offset = selection_position * 16
            total = self.sub_cdf[offset + 15]
            draw = self.targets.rng.random() * total
            start = bisect.bisect_left(self.sub_cdf, draw, offset, offset + 16) - offset
        for step in range(16):
            subnet = (start + step) % 16
            if self.sub_cursors[region_index * 16 + subnet] < 4096:
                return subnet
        return None

    def _next_c64(self, region_index, subnet):
        position = region_index * 16 + subnet
        screen_slot = self.screen_slots[region_index]
        while self.sub_cursors[position] < 4096:
            cursor = self.sub_cursors[position]
            self.sub_cursors[position] += 1
            relative = (self.prefixes[region_index] + 2053 * cursor) & 0xFFF
            slot = (subnet << 12) | relative
            if slot != screen_slot:
                return (self.prefixes[region_index] << 16) | slot
        return None

    def _fallback_target(self):
        """Find remaining capacity after a weighted choice reaches exhaustion."""
        for selection_position, region_index in enumerate(self.selected):
            for subnet in range(16):
                if self.sub_cursors[region_index * 16 + subnet] < 4096:
                    c64 = self._next_c64(region_index, subnet)
                    if c64 is not None:
                        return selection_position, region_index, subnet, c64
        return None

    def iter_targets(self, limit):
        if self.phase == "candidate_screen":
            count = min(limit, self.screen_budget - self.screen_generated)
            for _ in range(count):
                yield self._screen_target(), "candidate-screen", "candidate_screen"
            return

        quota = self._round_quota(self.round_index) if self.round_index < self.rounds else 0
        count = min(
            limit,
            self.allowance - self.sent,
            self.search_budget - self.search_generated,
            quota - self.round_generated,
        )
        produced = 0
        while produced < count and self.selected:
            selection_position, region_index = self._choose_region()
            subnet = self._choose_subnet(selection_position, region_index)
            c64 = self._next_c64(region_index, subnet) if subnet is not None else None
            if c64 is None:
                fallback = self._fallback_target()
                if fallback is None:
                    break
                selection_position, region_index, subnet, c64 = fallback
            mode = "uniform" if self.round_index == 0 else "adaptive"
            yield c64, f"{region_index}:{subnet}", mode
            self.region_draws[region_index] += 1
            self.search_generated += 1
            self.round_generated += 1
            produced += 1

    def feed_aggregate(self, node_str, mode, probes, positives, replies, sources):
        self.sent += probes
        self.positive_count += positives
        if mode == "candidate_screen":
            self.screen_sent += probes
            return
        region_text, subnet_text = node_str.split(":", 1)
        region = int(region_text)
        subnet = int(subnet_text)
        position = region * 16 + subnet
        self.region_probes[region] += probes
        self.region_hits[region] += positives
        self.round_probes[region] += probes
        self.round_hits[region] += positives
        self.sub_probes[position] += probes
        self.sub_hits[position] += positives
        self.round_committed += probes

    def finish_batch(self):
        if self.phase == "candidate_screen" and self.screen_sent == self.screen_budget:
            self._build_candidates()
            return
        if self.phase == "search" and self.round_index < self.rounds:
            quota = self._round_quota(self.round_index)
            if self.round_committed == quota:
                self.round_index += 1
                self.round_generated = 0
                self.round_committed = 0
                self._prepare_round()

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
            raise ValueError("TNet has no legacy checkpoint format")
        for key, value in state.items():
            if key != "native" and key not in {"frame", "targets", "allowance"}:
                setattr(self, key, value)
