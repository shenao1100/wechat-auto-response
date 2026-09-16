from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import math
import operator
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote

from .timeutils import get_timezone


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(float(right)) > 100:
            raise ValueError("exponent must be between -100 and 100")
        return _BINARY_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
        if node.keywords or len(node.args) > 2:
            raise ValueError("unsupported function arguments")
        return _FUNCTIONS[node.func.id](*(_evaluate(item) for item in node.args))
    raise ValueError("expression contains unsupported syntax")


def calculate(expression: str) -> dict[str, Any]:
    expression = str(expression).strip()
    if not expression or len(expression) > 200:
        raise ValueError("expression must contain 1 to 200 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid mathematical expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("expression is too complex")
    try:
        result = _evaluate(tree)
    except (ArithmeticError, OverflowError, TypeError) as exc:
        raise ValueError(f"calculation failed: {exc}") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("result must be finite")
    if abs(float(result)) > 1e100:
        raise ValueError("result is too large")
    return {"expression": expression, "result": result}


def date_time(
    action: str,
    timezone_name: str,
    value: str | None = None,
    other_value: str | None = None,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
) -> dict[str, Any]:
    timezone = get_timezone(timezone_name)

    def parse(raw: str | None) -> datetime:
        if not raw:
            return datetime.now(timezone)
        try:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("datetime values must use ISO 8601 format") from exc
        return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)

    start = parse(value)
    if action == "now":
        result = datetime.now(timezone)
        return {"timezone": timezone_name, "datetime": result.isoformat(), "unix_timestamp": int(result.timestamp())}
    if action == "add":
        offsets = (int(days), int(hours), int(minutes))
        if any(abs(item) > 1_000_000 for item in offsets):
            raise ValueError("date offset is too large")
        result = start + timedelta(days=offsets[0], hours=offsets[1], minutes=offsets[2])
        return {"timezone": timezone_name, "datetime": result.isoformat(), "unix_timestamp": int(result.timestamp())}
    if action == "difference":
        if not other_value:
            raise ValueError("other_datetime is required for difference")
        seconds = (parse(other_value) - start).total_seconds()
        return {"seconds": seconds, "minutes": seconds / 60, "hours": seconds / 3600, "days": seconds / 86400}
    raise ValueError("action must be now, add, or difference")


_UNIT_GROUPS = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344},
    "mass": {"mg": 0.000001, "g": 0.001, "kg": 1.0, "oz": 0.028349523125, "lb": 0.45359237, "t": 1000.0},
    "data": {"B": 1.0, "KB": 1000.0, "MB": 1000.0**2, "GB": 1000.0**3, "KiB": 1024.0, "MiB": 1024.0**2, "GiB": 1024.0**3},
    "time": {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "day": 86400.0},
}


def convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("value must be finite")
    temperatures = {"C", "F", "K"}
    if from_unit in temperatures or to_unit in temperatures:
        if from_unit not in temperatures or to_unit not in temperatures:
            raise ValueError("temperature units cannot be mixed with other dimensions")
        celsius = {"C": number, "F": (number - 32) * 5 / 9, "K": number - 273.15}[from_unit]
        result = {"C": celsius, "F": celsius * 9 / 5 + 32, "K": celsius + 273.15}[to_unit]
    else:
        units = next((group for group in _UNIT_GROUPS.values() if from_unit in group and to_unit in group), None)
        if units is None:
            raise ValueError(f"cannot convert {from_unit} to {to_unit}")
        result = number * units[from_unit] / units[to_unit]
    return {"value": number, "from_unit": from_unit, "to_unit": to_unit, "result": result}


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    if len(value) > 20_000:
        raise ValueError("text must not exceed 20000 characters")
    return value


def calculate_hash(text: str, algorithm: str = "sha256") -> dict[str, str]:
    value = _bounded_text(text)
    if algorithm not in {"sha256", "sha384", "sha512", "md5"}:
        raise ValueError("unsupported hash algorithm")
    digest = hashlib.new(algorithm, value.encode("utf-8")).hexdigest()
    return {"algorithm": algorithm, "encoding": "utf-8", "digest": digest}


def generate_random(
    kind: str,
    minimum: int = 0,
    maximum: int = 100,
    count: int = 1,
) -> dict[str, Any]:
    count = int(count)
    if not 1 <= count <= 20:
        raise ValueError("count must be between 1 and 20")
    if kind == "uuid":
        return {"kind": kind, "values": [str(uuid.uuid4()) for _ in range(count)]}
    if kind == "integer":
        minimum, maximum = int(minimum), int(maximum)
        if minimum > maximum:
            raise ValueError("minimum must not exceed maximum")
        if maximum - minimum > 10**18:
            raise ValueError("random integer range is too large")
        return {
            "kind": kind,
            "values": [minimum + secrets.randbelow(maximum - minimum + 1) for _ in range(count)],
        }
    raise ValueError("kind must be uuid or integer")


def encode_decode(action: str, text: str) -> dict[str, str]:
    value = _bounded_text(text)
    if action == "url_encode":
        result = quote(value, safe="")
    elif action == "url_decode":
        result = unquote(value)
    elif action == "base64_encode":
        result = base64.b64encode(value.encode("utf-8")).decode("ascii")
    elif action == "base64_decode":
        try:
            result = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("text is not valid UTF-8 Base64") from exc
    else:
        raise ValueError("unsupported encode/decode action")
    return {"action": action, "result": result}
