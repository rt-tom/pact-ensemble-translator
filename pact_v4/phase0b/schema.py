"""Minimal in-repo JSON-schema validator.

Only supports the constructs we use in ``v4_golden_record.schema.json``.
Adding a new construct there requires teaching this validator too; unknown
keywords are ignored (permissive draft-2020-12 semantics), but unsupported
``type`` values or non-local ``$ref`` targets raise loudly so we cannot
silently mis-validate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "schemas" / "v4_golden_record.schema.json"
)


class SchemaError(ValueError):
    """Raised when the schema itself references features we do not support."""


_TYPE_TO_PY: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def load_schema(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SCHEMA_PATH).read_text(encoding="utf-8"))


def validate(instance: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """Return a list of human-readable error messages; empty list = valid."""
    schema = schema if schema is not None else load_schema()
    defs = schema.get("$defs", {})
    errors: list[str] = []
    _validate(instance, schema, "$", defs, errors)
    return errors


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" not in node:
        return node
    ref = node["$ref"]
    if not ref.startswith("#/$defs/"):
        raise SchemaError(f"Only local #/$defs/ refs supported: {ref}")
    name = ref[len("#/$defs/"):]
    if name not in defs:
        raise SchemaError(f"Unknown $def: {name}")
    return defs[name]


def _check_type(value: Any, expected: str, path: str, errors: list[str]) -> bool:
    py = _TYPE_TO_PY.get(expected)
    if py is None:
        raise SchemaError(f"Unsupported type: {expected}")
    # bool is a subclass of int in Python; do not treat True/False as integer
    # or number matches.
    if isinstance(value, bool) and expected in {"integer", "number"}:
        errors.append(f"{path}: expected {expected}, got boolean")
        return False
    if not isinstance(value, py):
        errors.append(
            f"{path}: expected {expected}, got {type(value).__name__}"
        )
        return False
    return True


def _validate(
    value: Any,
    node: dict[str, Any],
    path: str,
    defs: dict[str, Any],
    errors: list[str],
) -> None:
    node = _resolve(node, defs)

    if "const" in node:
        if value != node["const"]:
            errors.append(
                f"{path}: expected const {node['const']!r}, got {value!r}"
            )
        return

    if "enum" in node:
        if value not in node["enum"]:
            errors.append(f"{path}: {value!r} not in enum {node['enum']}")
        return

    if "type" in node and not _check_type(value, node["type"], path, errors):
        return

    if isinstance(value, str):
        if "pattern" in node and not re.search(node["pattern"], value):
            errors.append(
                f"{path}: {value!r} does not match pattern {node['pattern']!r}"
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(
                f"{path}: {value} < minimum {node['minimum']}"
            )
        if "maximum" in node and value > node["maximum"]:
            errors.append(
                f"{path}: {value} > maximum {node['maximum']}"
            )

    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(
                f"{path}: length {len(value)} < minItems {node['minItems']}"
            )
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(
                f"{path}: length {len(value)} > maxItems {node['maxItems']}"
            )
        if "items" in node:
            for i, item in enumerate(value):
                _validate(item, node["items"], f"{path}[{i}]", defs, errors)

    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required")
        for key, sub in node.get("properties", {}).items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", defs, errors)
