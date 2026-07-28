"""Minimal in-repo JSON-schema validator for the Phase 1A contract schemas.

Deliberately self-contained (no dependency on pact_v4.phase0b, no new
external dependency): Phase 1 contracts should not depend on the Phase 0B
golden-set tooling module, and the project avoids adding a `jsonschema`
requirement for a handful of draft-07/2020-12 documents that only use a
small, known subset of keywords.

Supports: type, const, enum, pattern, minimum/maximum, minItems/maxItems,
uniqueItems, items (list-form tuple validation and single-schema form),
required, properties, additionalProperties=false, and local `$ref`/`$defs` resolution. Unsupported
schema constructs raise ``SchemaError`` loudly rather than silently
under-validating.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class SchemaError(ValueError):
    """Raised when the schema itself uses a feature this validator lacks."""


_TYPE_TO_PY: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def load_schema(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable error messages; empty list = valid."""
    defs = schema.get("$defs", schema.get("definitions", {}))
    errors: list[str] = []
    _validate(instance, schema, "$", defs, errors)
    return errors


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" not in node:
        return node
    ref = node["$ref"]
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            name = ref[len(prefix):]
            if name not in defs:
                raise SchemaError(f"Unknown $def: {name}")
            return defs[name]
    raise SchemaError(f"Only local #/$defs or #/definitions refs supported: {ref}")


def _check_type(value: Any, expected: str, path: str, errors: list[str]) -> bool:
    py = _TYPE_TO_PY.get(expected)
    if py is None:
        raise SchemaError(f"Unsupported type: {expected}")
    if isinstance(value, bool) and expected in {"integer", "number"}:
        errors.append(f"{path}: expected {expected}, got boolean")
        return False
    if not isinstance(value, py):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
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
            errors.append(f"{path}: expected const {node['const']!r}, got {value!r}")
        return

    if "enum" in node:
        if value not in node["enum"]:
            errors.append(f"{path}: {value!r} not in enum {node['enum']}")
        return

    if "type" in node and not _check_type(value, node["type"], path, errors):
        return

    if isinstance(value, str) and "pattern" in node and not re.search(node["pattern"], value):
        errors.append(f"{path}: {value!r} does not match pattern {node['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: {value} < minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path}: {value} > maximum {node['maximum']}")

    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(f"{path}: length {len(value)} < minItems {node['minItems']}")
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(f"{path}: length {len(value)} > maxItems {node['maxItems']}")
        if node.get("uniqueItems"):
            seen = []
            for item in value:
                if item in seen:
                    errors.append(f"{path}: duplicate item {item!r} violates uniqueItems")
                seen.append(item)
        items_schema = node.get("items")
        if isinstance(items_schema, list):
            for i, (item, sub) in enumerate(zip(value, items_schema)):
                _validate(item, sub, f"{path}[{i}]", defs, errors)
        elif isinstance(items_schema, dict):
            for i, item in enumerate(value):
                _validate(item, items_schema, f"{path}[{i}]", defs, errors)

    if isinstance(value, dict):
        if node.get("additionalProperties") is False:
            allowed = set(node.get("properties", {}))
            for key in sorted(value.keys() - allowed):
                errors.append(f"{path}.{key}: unexpected property")
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required")
        for key, sub in node.get("properties", {}).items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", defs, errors)
