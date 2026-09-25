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
    kind: str  # "breaking" | "additive" | "index-breaking" (ADR-004 §4)
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


def write_snapshot(name: str, surface: dict, version: str, extra: dict | None = None) -> Path:
    CONTRACTS_DIR.mkdir(exist_ok=True)
    path = CONTRACTS_DIR / f"{name}.json"
    body = {"source_version": version, "surface": surface, **(extra or {})}
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


# ── Index-schema surface (ADR-004 §4, research R9) ──────────────────────────
# A committed snapshot of the vector- and graph-store schemas as of the last
# release, used to classify a schema-changing PR as "index-breaking" before
# it ships. Every difference here — however small — invalidates persisted
# index data (constitution VII), so `diff_index` never calls anything
# additive.

_DIM_PLACEHOLDER = "<VECTOR_DIM>"
_PROBE_DIM = 999999997  # an unlikely-to-collide stand-in, always replaced below


def _milvus_index_surface(dim: int) -> dict:
    from treeweft.adapters.milvus.vector_store import INDEX_PARAMS, build_collection_schema

    schema = build_collection_schema(dim)
    fields = []
    for f in schema.fields:
        entry: dict = {
            "name": f.name,
            "dtype": f.dtype.name,
            "is_primary": bool(f.is_primary),
            "auto_id": bool(f.auto_id),
        }
        params = dict(getattr(f, "params", None) or {})
        if "dim" in params:
            params["dim"] = _DIM_PLACEHOLDER
        if params:
            entry["params"] = params
        max_length = getattr(f, "max_length", None)
        if max_length is not None:
            entry["max_length"] = max_length
        fields.append(entry)
    functions = [
        {
            "name": fn.name,
            "type": fn.type.name,
            "input_fields": list(fn.input_field_names),
            "output_fields": list(fn.output_field_names),
        }
        for fn in schema.functions
    ]
    return {
        "fields": fields,
        "functions": functions,
        "index_params": [dict(spec) for spec in INDEX_PARAMS],
        "stamp_properties": ["treeweft.index_schema", "treeweft.embedding_model"],
    }


def _lancedb_index_surface(dim: int) -> dict:
    from treeweft.adapters.lancedb.vector_store import FTS_COLUMN, _schema

    schema = _schema(dim)
    fields = [
        {
            "name": f.name,
            "type": str(f.type).replace(str(dim), _DIM_PLACEHOLDER),
            "nullable": bool(f.nullable),
        }
        for f in schema
    ]
    return {
        "fields": fields,
        "fts_column": FTS_COLUMN,
        "stamp_field_metadata_on": "vector",
        "stamp_keys": ["treeweft.index_schema", "treeweft.embedding_model"],
    }


def _chromadb_index_surface() -> dict:
    from treeweft.adapters.chromadb import vector_store as chroma

    return {
        "stamp_metadata_keys": [
            chroma._STAMP_SCHEMA_KEY,
            chroma._STAMP_MODEL_KEY,
            chroma._STAMP_DIM_KEY,
        ],
    }


def _neo4j_index_surface() -> dict:
    from treeweft.adapters.neo4j import graph_store as neo4j

    return {"schema_statements": list(neo4j._SCHEMA_STATEMENTS), "meta_node_label": "TreeweftMeta"}


def _sqlite_index_surface() -> dict:
    from treeweft.adapters.sqlite import graph_store as sqlite

    return {"schema_statements": list(sqlite._SCHEMA_STATEMENTS), "meta_table": "treeweft_meta"}


def index_schema_surface() -> dict:
    """The current index schema across every backend, with every dimension
    replaced by a fixed placeholder so `.env`'s `VECTOR_DIM` never leaks
    into the snapshot. Imports every adapter lazily, inside this function —
    `infrastructure.contracts` must stay off the startup path (CLAUDE.md:
    never import the Milvus/Neo4j adapters from startup-path code)."""
    return {
        "milvus": _milvus_index_surface(_PROBE_DIM),
        "lancedb": _lancedb_index_surface(_PROBE_DIM),
        "chromadb": _chromadb_index_surface(),
        "neo4j": _neo4j_index_surface(),
        "sqlite": _sqlite_index_surface(),
    }


def diff_index(old: dict, new: dict) -> list[Change]:
    """Any difference in the index schema is index-breaking (ADR-004 §4) —
    it can invalidate persisted data, so there is no "additive" case."""
    changes: list[Change] = []
    for backend in sorted(set(old) | set(new)):
        o, n = old.get(backend), new.get(backend)
        if o != n:
            changes.append(Change("index-breaking", backend, "index schema changed"))
    return changes


def index_version_error(recorded_schema: int, current_schema: int, released: str, current: str) -> str | None:
    """ADR-004 §4: an index-schema change needs both `INDEX_SCHEMA_VERSION`
    raised and the SemVer major bumped over the last release."""
    r_major = parse_semver(released)[0]
    c_major = parse_semver(current)[0]
    schema_ok = current_schema > recorded_schema
    major_ok = c_major > r_major
    if schema_ok and major_ok:
        return None
    return (
        f"INDEX_SCHEMA_VERSION must exceed {recorded_schema} and the major must exceed "
        f"{r_major} (at least {r_major + 1}.0.0)"
    )
