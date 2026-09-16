from __future__ import annotations

import unittest

from wechat_agent.agent_tools import ToolRuntime, tool_definitions
from wechat_agent.timeutils import get_timezone
from wechat_agent.utility_tools import (
    calculate,
    calculate_hash,
    convert_units,
    date_time,
    encode_decode,
    generate_random,
)


class UtilityToolTests(unittest.TestCase):
    def test_calculator_handles_arithmetic_and_functions(self) -> None:
        self.assertEqual(calculate("2 + 3 * 4")["result"], 14)
        self.assertEqual(calculate("sqrt(81) + abs(-3)")["result"], 12)

    def test_calculator_rejects_code_and_extreme_exponents(self) -> None:
        with self.assertRaises(ValueError):
            calculate("__import__('os').system('whoami')")
        with self.assertRaises(ValueError):
            calculate("2 ** 101")

    def test_date_time_add_and_difference(self) -> None:
        added = date_time("add", "Asia/Shanghai", "2026-09-16T08:00:00", days=1, minutes=30)
        self.assertEqual(added["datetime"], "2026-09-17T08:30:00+08:00")

        difference = date_time(
            "difference",
            "Asia/Shanghai",
            "2026-09-16T08:00:00",
            "2026-09-17T10:00:00",
        )
        self.assertEqual(difference["hours"], 26)

    def test_unit_conversion(self) -> None:
        self.assertEqual(convert_units(1.5, "km", "m")["result"], 1500)
        self.assertAlmostEqual(convert_units(32, "F", "C")["result"], 0)
        with self.assertRaises(ValueError):
            convert_units(1, "kg", "km")

    def test_hash_random_and_encoding_tools(self) -> None:
        self.assertEqual(
            calculate_hash("abc")["digest"],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        values = generate_random("integer", 10, 20, 5)["values"]
        self.assertEqual(len(values), 5)
        self.assertTrue(all(10 <= value <= 20 for value in values))
        encoded = encode_decode("base64_encode", "微信")["result"]
        self.assertEqual(encode_decode("base64_decode", encoded)["result"], "微信")
        self.assertEqual(encode_decode("url_decode", encode_decode("url_encode", "a b")["result"])["result"], "a b")

    def test_tools_are_exposed_to_the_agent(self) -> None:
        names = {item["function"]["name"] for item in tool_definitions()}
        self.assertTrue(
            {
                "calculator", "date_time", "convert_units", "calculate_hash",
                "generate_random", "encode_decode", "web_search", "fetch_web_pages",
            }.issubset(names)
        )

    def test_runtime_dispatches_utility_tools(self) -> None:
        runtime = ToolRuntime.__new__(ToolRuntime)
        runtime.timezone = get_timezone("Asia/Shanghai")
        calculation = runtime.execute("calculator", {"expression": "6 * 7"}, None)
        conversion = runtime.execute(
            "convert_units",
            {"value": 2, "from_unit": "h", "to_unit": "min"},
            None,
        )
        self.assertEqual(calculation.content, {"ok": True, "expression": "6 * 7", "result": 42})
        self.assertEqual(conversion.content["result"], 120)


if __name__ == "__main__":
    unittest.main()
