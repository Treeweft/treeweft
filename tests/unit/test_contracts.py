"""SemVer bumps are checked against contract snapshots of the last release (ADR-004 §4)."""
import pytest

from treeweft import versions
from treeweft.infrastructure import contracts as c

OBJ = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
STR, INT = {"type": "string"}, {"type": "integer"}


def kinds(changes):
    return sorted((ch.kind, ch.where) for ch in changes)


# ── diff_schema: inputs (request bodies / parameters / tool arguments) ──

def test_identical_schemas_have_no_changes():
    assert c.diff_schema(OBJ({"q": STR}, ["q"]), OBJ({"q": STR}, ["q"]), "x", "input") == []


def test_new_optional_input_is_additive():
    assert kinds(c.diff_schema(OBJ({"q": STR}), OBJ({"q": STR, "k": INT}), "x", "input")) == [("additive", "x.k")]


def test_new_required_input_is_breaking():
    assert kinds(c.diff_schema(OBJ({"q": STR}), OBJ({"q": STR, "k": INT}, ["k"]), "x", "input")) == [("breaking", "x.k")]


def test_removed_input_is_breaking():
    assert kinds(c.diff_schema(OBJ({"q": STR, "k": INT}), OBJ({"q": STR}), "x", "input")) == [("breaking", "x.k")]


def test_input_becoming_required_is_breaking_and_optional_is_additive():
    assert kinds(c.diff_schema(OBJ({"q": STR}), OBJ({"q": STR}, ["q"]), "x", "input")) == [("breaking", "x.q")]
    assert kinds(c.diff_schema(OBJ({"q": STR}, ["q"]), OBJ({"q": STR}), "x", "input")) == [("additive", "x.q")]


def test_type_change_is_breaking():
    assert kinds(c.diff_schema(OBJ({"q": STR}), OBJ({"q": INT}), "x", "input")) == [("breaking", "x.q")]


# ── diff_schema: outputs (responses / tool results) ──

def test_new_output_field_is_additive_and_removed_is_breaking():
    assert kinds(c.diff_schema(OBJ({"a": STR}), OBJ({"a": STR, "b": INT}), "r", "output")) == [("additive", "r.b")]
    assert kinds(c.diff_schema(OBJ({"a": STR, "b": INT}), OBJ({"a": STR}), "r", "output")) == [("breaking", "r.b")]


def test_output_no_longer_guaranteed_is_breaking():
    assert kinds(c.diff_schema(OBJ({"a": STR}, ["a"]), OBJ({"a": STR}), "r", "output")) == [("breaking", "r.a")]


def test_enum_values_removed_is_breaking_added_is_additive():
    old, new = {"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["b", "c"]}
    assert kinds(c.diff_schema(old, new, "e", "input")) == [("additive", "e"), ("breaking", "e")]


def test_default_change_is_additive():
    assert kinds(c.diff_schema({"type": "integer", "default": 5}, {"type": "integer", "default": 10}, "d", "input")) == [("additive", "d")]


def test_unrecognised_change_is_conservatively_breaking():
    old = {"anyOf": [STR, {"type": "null"}]}
    new = {"anyOf": [INT, {"type": "null"}]}
    changes = c.diff_schema(old, new, "u", "input")
    assert kinds(changes) == [("breaking", "u")]
    assert "unclassified" in changes[0].what


# ── surfaces ──

def test_property_named_like_noise_is_kept():
    openapi = {"paths": {"/x": {"post": {"requestBody": {"content": {"application/json": {"schema": {
        "type": "object", "description": "drop me",
        "properties": {"title": STR, "description": STR}, "required": ["title"]}}}}}}}}
    body = c.api_surface(openapi)["POST /x"]["body"]
    assert "description" not in body
    assert set(body["properties"]) == {"title", "description"}


def test_api_surface_resolves_refs_and_classifies_endpoints():
    base = {"components": {"schemas": {"Req": OBJ({"q": STR}, ["q"])}},
            "paths": {"/search": {"post": {"requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Req"}}}}}}}}
    old = c.api_surface(base)
    assert old["POST /search"]["body"] == OBJ({"q": STR}, ["q"])

    added = {**base, "paths": {**base["paths"], "/health": {"get": {}}}}
    assert kinds(c.diff_api(old, c.api_surface(added))) == [("additive", "GET /health")]
    assert kinds(c.diff_api(c.api_surface(added), old)) == [("breaking", "GET /health")]


def test_query_parameters_are_compared_like_fields():
    def op(required):
        return {"paths": {"/jobs": {"get": {"parameters": [{"name": "limit", "in": "query", "required": required, "schema": INT}]}}}}
    assert kinds(c.diff_api(c.api_surface(op(False)), c.api_surface(op(True)))) == [("breaking", "GET /jobs params.limit")]


def test_first_documented_response_schema_is_additive():
    def op(schema):
        return {"paths": {"/s": {"get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}}}}
    assert kinds(c.diff_api(c.api_surface(op({})), c.api_surface(op(OBJ({"a": STR}))))) == [("additive", "GET /s response")]


def test_mcp_tool_added_and_removed():
    old = {"search_code": {"input": OBJ({"query": STR}, ["query"]), "output": {}}}
    new = {**old, "new_tool": {"input": OBJ({}), "output": {}}}
    assert kinds(c.diff_mcp(old, new)) == [("additive", "tool new_tool")]
    assert kinds(c.diff_mcp(new, old)) == [("breaking", "tool new_tool")]


# ── version rules ──

@pytest.mark.parametrize("released,current,bump,ok", [
    ("1.0.0", "1.0.0", None, True),
    ("1.0.0", "1.0.1", None, True),
    ("1.0.0", "1.0.1", "minor", False),
    ("1.0.0", "1.1.0", "minor", True),
    ("1.0.0", "1.9.0", "major", False),
    ("1.0.0", "2.0.0", "major", True),
    ("1.4.0", "1.3.9", None, False),  # never go backwards
])
def test_version_error(released, current, bump, ok):
    assert (c.version_error(released, current, bump) is None) is ok


def test_required_bump():
    assert c.required_bump([]) is None
    assert c.required_bump([c.Change("additive", "a", "b")]) == "minor"
    assert c.required_bump([c.Change("additive", "a", "b"), c.Change("breaking", "c", "d")]) == "major"


# ── the real check against the committed snapshots ──

def test_current_surface_is_deterministic():
    assert c.current_api_surface() == c.current_api_surface()
    assert c.current_mcp_surface() == c.current_mcp_surface()


def test_pyproject_version_satisfies_the_released_contracts():
    failures = []
    for name, current, differ in (
        ("api", c.current_api_surface(), c.diff_api),
        ("mcp_tools", c.current_mcp_surface(), c.diff_mcp),
    ):
        snapshot = c.load_snapshot(name)
        changes = differ(snapshot["surface"], current)
        error = c.version_error(snapshot["source_version"], versions.SOURCE_VERSION, c.required_bump(changes))
        if error:
            failures.append(f"contracts/{name}.json: {error}\n" + "\n".join(f"  {ch}" for ch in changes))
    assert not failures, (
        "\n".join(failures)
        + "\nBump the version in pyproject.toml. Regenerate the snapshots only in a release PR "
        "(python scripts/update_contracts.py)."
    )
