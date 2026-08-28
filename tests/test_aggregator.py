from __future__ import annotations

import threading
import unittest

from wechat_agent.aggregator import MessageAggregator
from wechat_agent.models import GroupConfig


class AggregatorTests(unittest.TestCase):
    def test_high_volume_is_split_into_bounded_batches(self):
        group = GroupConfig("g1", "Group", ("target",), aggregation_seconds=0.01)
        aggregator = MessageAggregator(max_messages=2)
        stop = threading.Event()
        aggregator.submit(group, {"id": 1})
        aggregator.submit(group, {"id": 2})
        aggregator.submit(group, {"id": 3})

        first = aggregator.pop_ready(stop)
        second = aggregator.pop_ready(stop)
        self.assertEqual([item["id"] for item in first.messages], [1, 2])
        self.assertEqual([item["id"] for item in second.messages], [3])


if __name__ == "__main__":
    unittest.main()
