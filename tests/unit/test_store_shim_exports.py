"""Every backend defines every name its shim promises (ADR-004 §3).

Graph: `treeweft.graph_store._EXPORTED` must be defined on both the sqlite
and neo4j adapter modules directly (plain import — neither adapter connects
at import time). Vector: `observe_index`/`write_stamp`/`sample_chunks`/
`drop_index` must be reachable through `treeweft.retriever` under every
`VECTOR_STORE` value, checked in a subprocess per value (the dispatcher
picks its backend at import time, and this process already imported one).
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest


class TestGraphBackendsExportEverything:
    def test_sqlite_defines_every_exported_name(self):
        from treeweft.adapters.sqlite import graph_store as impl
        from treeweft.graph_store import _EXPORTED
        missing = [name for name in _EXPORTED if not hasattr(impl, name)]
        assert not missing, f"sqlite graph_store is missing: {missing}"

    def test_neo4j_defines_every_exported_name(self):
        from treeweft.adapters.neo4j import graph_store as impl
        from treeweft.graph_store import _EXPORTED
        missing = [name for name in _EXPORTED if not hasattr(impl, name)]
        assert not missing, f"neo4j graph_store is missing: {missing}"


_VECTOR_STAMP_PROBE = textwrap.dedent(
    """
    import json
    import treeweft.retriever as r
    names = ["observe_index", "write_stamp", "sample_chunks", "drop_index"]
    print(json.dumps({n: hasattr(r, n) for n in names}))
    """
)


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _VECTOR_STAMP_PROBE],
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("vector_store", ["milvus", "lancedb", "chromadb"])
def test_vector_backend_exports_stamp_functions_through_retriever(vector_store):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["VECTOR_STORE"] = vector_store
    res = _run(env)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout.strip().splitlines()[-1])
    missing = [name for name, present in got.items() if not present]
    assert not missing, f"VECTOR_STORE={vector_store!r} retriever is missing: {missing}"
