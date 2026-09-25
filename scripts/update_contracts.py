#!/usr/bin/env python3
"""Regenerate contracts/*.json from the current code.

Run this ONLY in a release PR, after setting the release's SemVer in
pyproject.toml (ADR-004 §4). The snapshots record the surface as of the last
release; regenerating them in a feature PR would hide that PR's changes from
the SemVer check.
"""
from treeweft import versions
from treeweft.infrastructure import contracts


def main() -> None:
    for name, surface in (
        ("api", contracts.current_api_surface()),
        ("mcp_tools", contracts.current_mcp_surface()),
    ):
        path = contracts.write_snapshot(name, surface, versions.SOURCE_VERSION)
        print(f"wrote {path} at {versions.SOURCE_VERSION}")

    path = contracts.write_snapshot(
        "index_schema",
        contracts.index_schema_surface(),
        versions.SOURCE_VERSION,
        extra={"index_schema": versions.INDEX_SCHEMA_VERSION},
    )
    print(f"wrote {path} at {versions.SOURCE_VERSION} (index_schema={versions.INDEX_SCHEMA_VERSION})")


if __name__ == "__main__":
    main()
