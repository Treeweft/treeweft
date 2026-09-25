"""Contract snapshots and SemVer classification (ADR-004 §4).

contracts/*.json hold the indexer HTTP API and MCP tool surface as of the last
release. tests/unit/test_contracts.py rebuilds the current surface, classifies
each difference as breaking (needs a MAJOR bump) or additive (needs a MINOR
bump), and fails when pyproject.toml's version doesn't cover it. Unrecognised
schema differences are classified as breaking, deliberately.

Known limitation: FastAPI documents a response schema only where an endpoint
declares a response_model. Responses returned as plain dicts have the schema
{}, so field changes inside them are invisible to this check.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from treeweft.versions import parse_semver

# src/treeweft/infrastructure/contracts.py -> parents[3] is the repository root.
CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"

_NOISE = {"title", "description", "examples", "example"}
_METHODS = ("get", "post", "put", "patch", "delete")
_HANDLED = {"type", "properties", "required", "enum", "items", "default"}


@dataclass(frozen=True)
class Change:
    kind: str  # "breaking" | "additive"
    where: str
    what: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.where}: {self.what}"


def _strip(node):
    """Drop documentation-only keys, but never a property that happens to be named like one."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in _NOISE:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: _strip(schema) for name, schema in value.items()}
            else:
                out[key] = _strip(value)
        return out
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _resolve(node, defs: dict, seen: tuple = ()):
    """Inline $ref'd schemas; a recursive reference stays as {"$ref": name}."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name in seen or name not in defs:
                return {"$ref": name}
            return _resolve(defs[name], defs, seen + (name,))
        return {k: _resolve(v, defs, seen) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve(v, defs, seen) for v in node]
    return node


def _key(value) -> str:
    return json.dumps(value, sort_keys=True)


def diff_schema(old, new, where: str, direction: str) -> list[Change]:
    """Classify the difference between two JSON schemas.

    direction="input": the client sends this (request body, parameters, tool arguments).
    direction="output": the client receives this (response body, tool result).
    """
    if old == new:
        return []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [Change("breaking", where, "schema replaced")]
    if old.get("type") != new.get("type"):
        return [Change("breaking", where, f"type {old.get('type')!r} -> {new.get('type')!r}")]

    changes: list[Change] = []
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    old_req, new_req = set(old.get("required", [])), set(new.get("required", []))
    for name in sorted(old_props.keys() - new_props.keys()):
        changes.append(Change("breaking", f"{where}.{name}", "removed"))
    for name in sorted(new_props.keys() - old_props.keys()):
        if direction == "input" and name in new_req:
            changes.append(Change("breaking", f"{where}.{name}", "added as required"))
        else:
            changes.append(Change("additive", f"{where}.{name}", "added"))
    for name in sorted(old_props.keys() & new_props.keys()):
        changes += diff_schema(old_props[name], new_props[name], f"{where}.{name}", direction)
        was, now = name in old_req, name in new_req
        if was != now:
            # input: newly required breaks senders; output: no longer guaranteed breaks readers
            tightened = now if direction == "input" else was
            changes.append(Change(
                "breaking" if tightened else "additive", f"{where}.{name}",
                "now required" if now else "no longer required",
            ))

    if "enum" in old or "enum" in new:
        old_enum = {_key(v) for v in old.get("enum", [])}
        new_enum = {_key(v) for v in new.get("enum", [])}
        if old_enum - new_enum:
            changes.append(Change("breaking", where, f"enum values removed: {sorted(old_enum - new_enum)}"))
        if new_enum - old_enum:
            changes.append(Change("additive", where, f"enum values added: {sorted(new_enum - old_enum)}"))
    if old.get("items") != new.get("items"):
        changes += diff_schema(old.get("items"), new.get("items"), f"{where}[]", direction)
    if old.get("default") != new.get("default"):
        changes.append(Change("additive", where, f"default {old.get('default')!r} -> {new.get('default')!r}"))

    rest_old = {k: v for k, v in old.items() if k not in _HANDLED}
    rest_new = {k: v for k, v in new.items() if k not in _HANDLED}
    if rest_old != rest_new:
        changes.append(Change("breaking", where, "unclassified schema change (treated as breaking)"))
    return changes


def api_surface(openapi: dict) -> dict:
    defs = openapi.get("components", {}).get("schemas", {})
    surface = {}
    for path, item in openapi.get("paths", {}).items():
        for method in _METHODS:
            op = item.get(method)
            if op is None:
                continue
            params = {"type": "object", "properties": {}, "required": []}
            for p in op.get("parameters", []):
                params["properties"][p["name"]] = {**p.get("schema", {}), "x-in": p["in"]}
                if p.get("required"):
                    params["required"].append(p["name"])
            params["required"].sort()
            body = op.get("requestBody") or {}
            ok = next((r for code, r in sorted(op.get("responses", {}).items()) if code.startswith("2")), {})
            surface[f"{method.upper()} {path}"] = {
                "params": params,
                "body": body.get("content", {}).get("application/json", {}).get("schema"),
                "body_required": bool(body.get("required")),
                "response": (ok or {}).get("content", {}).get("application/json", {}).get("schema"),
            }
    return _strip(_resolve(surface, defs))


def mcp_surface(tools) -> dict:
    surface = {}
    for tool in sorted(tools, key=lambda t: t.name):
        output = tool.outputSchema or {}
        surface[tool.name] = {
            "input": _resolve(tool.inputSchema, tool.inputSchema.get("$defs", {})),
            "output": _resolve(output, output.get("$defs", {})),
        }
    return _strip(surface)


def current_api_surface() -> dict:
    from treeweft.application.indexer_service import app

    return api_surface(app.openapi())


def current_mcp_surface() -> dict:
    from treeweft.application.mcp_server import mcp

    return mcp_surface(asyncio.run(mcp.list_tools()))


def diff_api(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    for ep in sorted(old.keys() - new.keys()):
        changes.append(Change("breaking", ep, "endpoint removed"))
    for ep in sorted(new.keys() - old.keys()):
        changes.append(Change("additive", ep, "endpoint added"))
    for ep in sorted(old.keys() & new.keys()):
        o, n = old[ep], new[ep]
        changes += diff_schema(o["params"], n["params"], f"{ep} params", "input")
        if o["body"] is None and n["body"] is not None:
            changes.append(Change("breaking" if n["body_required"] else "additive", f"{ep} body", "request body added"))
        elif o["body"] is not None and n["body"] is None:
            changes.append(Change("breaking", f"{ep} body", "request body removed"))
        else:
            changes += diff_schema(o["body"], n["body"], f"{ep} body", "input")
            if n["body_required"] and not o["body_required"]:
                changes.append(Change("breaking", f"{ep} body", "request body now required"))
        if not o["response"] and n["response"]:
            changes.append(Change("additive", f"{ep} response", "response schema documented"))
        elif o["response"] and not n["response"]:
            changes.append(Change("breaking", f"{ep} response", "response schema removed"))
        else:
            changes += diff_schema(o["response"], n["response"], f"{ep} response", "output")
    return changes


def diff_mcp(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(old.keys() - new.keys()):
        changes.append(Change("breaking", f"tool {name}", "tool removed"))
    for name in sorted(new.keys() - old.keys()):
        changes.append(Change("additive", f"tool {name}", "tool added"))
    for name in sorted(old.keys() & new.keys()):
        changes += diff_schema(old[name]["input"], new[name]["input"], f"tool {name} input", "input")
        changes += diff_schema(old[name]["output"], new[name]["output"], f"tool {name} output", "output")
    return changes


def required_bump(changes) -> str | None:
    kinds = {c.kind for c in changes}
    if "breaking" in kinds:
        return "major"
    if "additive" in kinds:
        return "minor"
    return None


def version_error(released: str, current: str, bump: str | None) -> str | None:
    r, c = parse_semver(released), parse_semver(current)
    if c < r:
        return f"version {current} is lower than the released {released}"
    if bump == "major" and c[0] <= r[0]:
        return (f"breaking changes require a major bump over {released} "
                f"(at least {r[0] + 1}.0.0); pyproject.toml says {current}")
    if bump == "minor" and c[:2] <= r[:2]:
        return (f"additive changes require a minor bump over {released} "
                f"(at least {r[0]}.{r[1] + 1}.0); pyproject.toml says {current}")
    return None


def load_snapshot(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / f"{name}.json").read_text())


def write_snapshot(name: str, surface: dict, version: str) -> Path:
    CONTRACTS_DIR.mkdir(exist_ok=True)
    path = CONTRACTS_DIR / f"{name}.json"
    path.write_text(json.dumps({"source_version": version, "surface": surface}, indent=2, sort_keys=True) + "\n")
    return path
