"""SemVer bumps are checked against contract snapshots of the last release (ADR-004 §4)."""
import pytest

from treeweft import versions
from treeweft.infrastructure import contracts as c

OBJ = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
STR, INT, NULL = {"type": "string"}, {"type": "integer"}, {"type": "null"}


def optional(schema, **outer):
    """Optional[T] as pydantic writes it."""
    return {"anyOf": [schema, NULL], **outer}


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


@pytest.mark.parametrize("direction", ["input", "output"])
def test_unrecognised_change_is_conservatively_breaking(direction):
    old = {"type": "string", "pattern": "^[a-z]+$"}
    new = {"type": "string", "pattern": "^[a-z0-9]+$"}
    changes = c.diff_schema(old, new, "u", direction)
    assert kinds(changes) == [("breaking", "u")]
    assert "unclassified" in changes[0].what


# ── diff_schema: Optional (pydantic writes Optional[T] as anyOf [T, null]) ──

def test_input_widened_to_optional_is_additive():
    changes = c.diff_schema(OBJ({"top_k": INT}), OBJ({"top_k": optional(INT)}), "body", "input")
    assert kinds(changes) == [("additive", "body.top_k")]
    assert changes[0].what == "now nullable"


def test_input_no_longer_optional_is_breaking():
    changes = c.diff_schema(OBJ({"top_k": optional(INT)}), OBJ({"top_k": INT}), "body", "input")
    assert kinds(changes) == [("breaking", "body.top_k")]
    assert changes[0].what == "no longer nullable"


def test_output_becoming_nullable_is_breaking_and_the_reverse_is_additive():
    assert kinds(c.diff_schema(OBJ({"a": STR}), OBJ({"a": optional(STR)}), "r", "output")) == [("breaking", "r.a")]
    assert kinds(c.diff_schema(OBJ({"a": optional(STR)}), OBJ({"a": STR}), "r", "output")) == [("additive", "r.a")]


def test_optional_with_a_different_type_is_breaking():
    changes = c.diff_schema(optional(STR), optional(INT), "u", "input")
    assert kinds(changes) == [("breaking", "u")]
    assert "'string' -> 'integer'" in changes[0].what


def test_widening_to_optional_does_not_hide_a_type_change():
    assert kinds(c.diff_schema(STR, optional(INT), "u", "input")) == [("additive", "u"), ("breaking", "u")]


def test_order_of_the_null_branch_does_not_matter():
    assert c.diff_schema({"anyOf": [INT, NULL]}, {"anyOf": [NULL, INT]}, "u", "input") == []
    assert kinds(c.diff_schema(INT, {"anyOf": [NULL, INT]}, "u", "input")) == [("additive", "u")]


def test_optional_with_a_default_as_pydantic_writes_it():
    old = {"type": "integer", "default": 10}
    new = optional(INT, default=None)
    assert kinds(c.diff_schema(old, new, "u", "input")) == [("additive", "u"), ("additive", "u")]


def test_optional_union_gaining_null_is_additive_for_inputs():
    old = {"anyOf": [STR, INT]}
    new = {"anyOf": [STR, INT, NULL]}
    assert kinds(c.diff_schema(old, new, "u", "input")) == [("additive", "u")]
    assert kinds(c.diff_schema(old, new, "u", "output")) == [("breaking", "u")]


# ── diff_schema: limits ──

LIMITS = [
    # key, tighter value, looser value
    ("maxLength", 100, 1000),
    ("maxItems", 10, 50),
    ("maxProperties", 5, 6),
    ("maximum", 100, 200),
    ("exclusiveMaximum", 1.0, 1.5),
    ("minLength", 3, 1),
    ("minItems", 1, 0),
    ("minProperties", 2, 1),
    ("minimum", 1, 0),
    ("exclusiveMinimum", 0.5, 0.0),
]


@pytest.mark.parametrize("key,tight,loose", LIMITS)
def test_looser_input_limit_is_additive_and_tighter_is_breaking(key, tight, loose):
    assert kinds(c.diff_schema({"type": "string", key: tight}, {"type": "string", key: loose}, "q", "input")) == [("additive", "q")]
    assert kinds(c.diff_schema({"type": "string", key: loose}, {"type": "string", key: tight}, "q", "input")) == [("breaking", "q")]


@pytest.mark.parametrize("key,tight,loose", LIMITS)
def test_looser_output_limit_is_breaking_and_tighter_is_additive(key, tight, loose):
    assert kinds(c.diff_schema({"type": "string", key: tight}, {"type": "string", key: loose}, "r", "output")) == [("breaking", "r")]
    assert kinds(c.diff_schema({"type": "string", key: loose}, {"type": "string", key: tight}, "r", "output")) == [("additive", "r")]


@pytest.mark.parametrize("key,tight,loose", LIMITS)
def test_removing_a_limit_loosens_and_adding_one_tightens(key, tight, loose):
    limited, free = {"type": "string", key: tight}, {"type": "string"}
    assert kinds(c.diff_schema(limited, free, "q", "input")) == [("additive", "q")]
    assert kinds(c.diff_schema(free, limited, "q", "input")) == [("breaking", "q")]
    assert kinds(c.diff_schema(limited, free, "r", "output")) == [("breaking", "r")]
    assert kinds(c.diff_schema(free, limited, "r", "output")) == [("additive", "r")]


def test_limit_change_names_the_limit_and_both_values():
    changes = c.diff_schema({"type": "string", "maxLength": 100}, {"type": "string", "maxLength": 1000}, "q", "input")
    assert changes[0].what == "maxLength 100 -> 1000"


def test_limit_that_is_not_a_number_is_conservatively_breaking():
    changes = c.diff_schema({"type": "string", "maxLength": 100}, {"type": "string", "maxLength": "1000"}, "q", "input")
    assert kinds(changes) == [("breaking", "q")]


def test_limit_inside_an_optional_input():
    old = optional({"type": "string", "maxLength": 100})
    new = optional({"type": "string", "maxLength": 1000})
    assert kinds(c.diff_schema(old, new, "q", "input")) == [("additive", "q")]


# ── diff_schema: anyOf branches ──

def test_new_optional_property_in_an_optional_model_is_additive():
    old = optional(OBJ({"a": STR}, ["a"]))
    new = optional(OBJ({"a": STR, "b": INT}, ["a"]))
    assert kinds(c.diff_schema(old, new, "x", "input")) == [("additive", "x.b")]


def test_new_optional_property_in_one_branch_of_a_union_is_additive():
    old = {"anyOf": [OBJ({"a": STR}), OBJ({"b": INT})]}
    new = {"anyOf": [OBJ({"a": STR}), OBJ({"b": INT, "c": STR})]}
    assert kinds(c.diff_schema(old, new, "x", "input")) == [("additive", "x<1>.c")]


def test_new_required_property_in_a_union_branch_is_breaking():
    old = {"anyOf": [OBJ({"a": STR}), OBJ({"b": INT})]}
    new = {"anyOf": [OBJ({"a": STR}), OBJ({"b": INT, "c": STR}, ["c"])]}
    assert kinds(c.diff_schema(old, new, "x", "input")) == [("breaking", "x<1>.c")]


@pytest.mark.parametrize("direction", ["input", "output"])
def test_union_with_a_different_number_of_branches_is_conservatively_breaking(direction):
    old = {"anyOf": [STR, INT]}
    new = {"anyOf": [STR, INT, {"type": "boolean"}]}
    changes = c.diff_schema(old, new, "u", direction)
    assert kinds(changes) == [("breaking", "u")]
    assert "unclassified" in changes[0].what


# ── diff_schema: what the Optional handling must not hide ──

@pytest.mark.parametrize("direction", ["input", "output"])
def test_key_on_both_the_optional_node_and_its_branch_does_not_hide_a_change(direction):
    """The outer keys are merged into the branch. Where both carry the same
    key with different values, merging would let the outer value stand in
    for the branch's, so such a schema is not merged and not classified."""
    old = optional({"type": "string", "maxLength": 10}, maxLength=10)
    new = optional({"type": "string", "maxLength": 5}, maxLength=10)
    assert kinds(c.diff_schema(old, new, "q", direction)) == [("breaking", "q")]


@pytest.mark.parametrize("direction,expected", [("input", "breaking"), ("output", "additive")])
def test_change_in_a_branch_that_cannot_be_merged_is_classified_in_the_branch(direction, expected):
    old = optional({"type": "string", "maxLength": 10}, maxLength=20)
    new = optional({"type": "string", "maxLength": 5}, maxLength=20)
    assert kinds(c.diff_schema(old, new, "q", direction)) == [(expected, "q<0>")]


def test_key_on_both_with_the_same_value_is_no_change():
    schema = optional({"type": "string", "maxLength": 10}, maxLength=10)
    assert c.diff_schema(schema, dict(schema), "q", "input") == []


def test_union_branch_is_named_by_its_place_in_the_schema():
    """The null branch is left out of the comparison, not out of the count:
    the label must point at the branch a reader finds in the schema."""
    old = {"anyOf": [NULL, OBJ({"a": STR}), OBJ({"b": INT})]}
    new = {"anyOf": [NULL, OBJ({"a": STR}), OBJ({"b": INT, "c": STR})]}
    assert kinds(c.diff_schema(old, new, "x", "input")) == [("additive", "x<2>.c")]


def test_anyof_that_is_not_a_list_is_conservatively_breaking():
    changes = c.diff_schema({"anyOf": "string"}, {"anyOf": "integer"}, "u", "input")
    assert kinds(changes) == [("breaking", "u")]
    assert "unclassified" in changes[0].what


# ── the reproductions from issue #32 ──

def test_issue_32_optional_input_needs_a_minor_bump():
    old = {"type": "object", "properties": {"top_k": {"type": "integer"}}, "required": []}
    new = {"type": "object", "properties": {"top_k": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}, "required": []}
    assert c.required_bump(c.diff_schema(old, new, "body", "input")) == "minor"


def test_issue_32_looser_limit_needs_a_minor_bump():
    changes = c.diff_schema({"type": "string", "maxLength": 100}, {"type": "string", "maxLength": 1000}, "q", "input")
    assert c.required_bump(changes) == "minor"


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


# ── index schema (ADR-004 §3/§4) ──

BASE_INDEX_SURFACE = {
    "milvus": {"fields": [{"name": "vector", "dtype": "FLOAT_VECTOR"}], "index_params": []},
    "lancedb": {"fields": [{"name": "vector", "type": "fixed_size_list<item: float>[<VECTOR_DIM>]"}]},
    "chromadb": {"stamp_metadata_keys": ["treeweft.index_schema"]},
    "neo4j": {"schema_statements": ["CREATE CONSTRAINT entity_id_unique IF NOT EXISTS ..."]},
    "sqlite": {"schema_statements": ["CREATE TABLE entities (...)"]},
}


def _mutate(surface, backend, **overrides):
    import copy
    mutated = copy.deepcopy(surface)
    mutated[backend] = {**mutated[backend], **overrides}
    return mutated


@pytest.mark.parametrize("backend,overrides,label", [
    ("milvus", {"fields": [{"name": "vector", "dtype": "FLOAT_VECTOR"}, {"name": "new_field", "dtype": "INT64"}]}, "field added"),
    ("milvus", {"fields": []}, "field removed"),
    ("milvus", {"fields": [{"name": "vector", "dtype": "FLOAT16_VECTOR"}]}, "field retyped"),
    ("milvus", {"index_params": [{"field_name": "vector", "index_type": "IVF_FLAT"}]}, "index param changed"),
    ("neo4j", {"schema_statements": []}, "constraint removed"),
    ("sqlite", {"schema_statements": ["CREATE TABLE entities (...)", "CREATE TABLE new_table (...)"]}, "column/table added"),
    ("lancedb", {"fields": [{"name": "vector", "type": "fixed_size_list<item: double>[<VECTOR_DIM>]"}]}, "lancedb schema changed"),
])
def test_any_index_schema_difference_is_index_breaking(backend, overrides, label):
    new = _mutate(BASE_INDEX_SURFACE, backend, **overrides)
    changes = c.diff_index(BASE_INDEX_SURFACE, new)
    assert changes, label
    assert all(ch.kind == "index-breaking" for ch in changes)
    assert any(ch.where == backend for ch in changes)


def test_identical_index_surfaces_have_no_changes():
    assert c.diff_index(BASE_INDEX_SURFACE, BASE_INDEX_SURFACE) == []


@pytest.mark.parametrize("recorded_schema,current_schema,released,current,ok", [
    (1, 1, "1.0.0", "1.1.0", False),   # schema unchanged, no index diff should call this, but even so: major not bumped
    (1, 2, "1.0.0", "1.1.0", False),   # schema bumped, major not bumped
    (1, 1, "1.0.0", "2.0.0", False),   # major bumped, schema not bumped
    (1, 2, "1.0.0", "2.0.0", True),    # both bumped
])
def test_index_version_error(recorded_schema, current_schema, released, current, ok):
    error = c.index_version_error(recorded_schema, current_schema, released, current)
    assert (error is None) is ok
    if error:
        assert str(recorded_schema) in error


def test_index_schema_surface_has_no_leaked_vector_dim():
    surface = c.index_schema_surface()
    dumped = str(surface)
    assert "<VECTOR_DIM>" in dumped
    import os
    configured_dim = os.environ.get("VECTOR_DIM")
    if configured_dim and configured_dim != "999999997":
        assert configured_dim not in dumped


def test_the_committed_index_snapshot_matches_the_code():
    snapshot = c.load_snapshot("index_schema")
    current = c.index_schema_surface()
    changes = c.diff_index(snapshot["surface"], current)
    assert not changes, (
        "\n".join(f"  {ch}" for ch in changes)
        + "\nBump INDEX_SCHEMA_VERSION and the SemVer major, and regenerate "
        "contracts/index_schema.json only in a release PR."
    )
    assert snapshot.get("index_schema") == versions.INDEX_SCHEMA_VERSION or snapshot["source_version"] == "1.0.0"


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
