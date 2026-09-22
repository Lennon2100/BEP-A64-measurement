import ipaddress
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.common import Targets
from strategies import journal, subrecon, tnet


class FormalStrategiesTest(unittest.TestCase):
    def setUp(self):
        root = ipaddress.ip_network("2001:db8::/60")
        child = ipaddress.ip_network("2001:db8::/64")
        self.frame = SimpleNamespace(roots=[root], prefixes=[root, child], descendants={root: 1, child: 0})

    def test_journal_base_probes_are_not_repeated_after_feedback(self):
        cfg = {"theta_b": 4, "step_bits": [1, 4], "information_weight": 0.25, "prior_strength": 2, "prior_decay": 0.85, "bgp_weight": 0.25, "root_budget_fraction": 0.5}
        strategy = journal.Strategy(self.frame, Targets("journal"), cfg, 20)
        self.assertEqual(strategy.base(self.frame.roots[0]), 2)
        batch = strategy.next_batch(2)
        self.assertEqual(len(batch), 2)
        self.assertEqual(len({row["c64"] for row in batch}), 2)
        strategy.feedback([{"is_observed_positive": "1"}, {"is_observed_positive": "0"}])
        following = strategy.next_batch(2)
        self.assertFalse({row["c64"] for row in batch} & {row["c64"] for row in following})

    def test_tnet_screen_feedback_precedes_search(self):
        cfg = {"screen_budget": 2, "rounds": 10, "top_k": 1, "temperature": 1}
        strategy = tnet.Strategy(self.frame, Targets("tnet"), cfg, 8)
        screen = strategy.next_batch(8)
        self.assertEqual(len(screen), 1)  # one /60 root is below /48
        self.assertTrue(all(row["stage"] == "screen" for row in screen))
        strategy.feedback([{"response_class": "slow_au", "icmp_source": "2001:db8::1", "is_observed_positive": "1"}])
        search = strategy.next_batch(2)
        self.assertTrue(all(row["stage"] == "search" for row in search))

    def test_subrecon_table_and_response_driven_refinement(self):
        strategy = subrecon.Strategy(self.frame, Targets("subrecon"), {}, 100)
        first = strategy.next_batch(4)
        self.assertEqual(len(first), 4)
        responses = [{"response_class": "slow_au", "icmp_source": "2001:db8::1", "is_observed_positive": "1"}, {"response_class": "slow_au", "icmp_source": "2001:db8::2", "is_observed_positive": "1"}]
        strategy.feedback(responses + [{"response_class": "timeout", "icmp_source": "", "is_observed_positive": "0"}] * 2)
        following = strategy.next_batch(2)
        self.assertTrue(following)
        self.assertTrue(all(row["node"] != str(self.frame.roots[0]) for row in following))


if __name__ == "__main__":
    unittest.main()
