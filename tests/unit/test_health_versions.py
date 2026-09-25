"""/health reports the source SemVer and the CalVer product release (ADR-004 §1)."""
import pytest

from treeweft import versions


@pytest.mark.asyncio
async def test_indexer_health_reports_versions(monkeypatch):
    monkeypatch.setenv("TREEWEFT_RELEASE", "2026.10.1")
    from treeweft.application import indexer_service

    body = await indexer_service.health()

    assert body["version"] == versions.SOURCE_VERSION
    assert body["release"] == "2026.10.1"
    # existing fields keep their meaning
    assert body["status"] == "ok"
    assert {"database", "auth_enabled"} <= body.keys()


@pytest.mark.asyncio
async def test_indexer_health_release_is_null_from_source(monkeypatch):
    monkeypatch.delenv("TREEWEFT_RELEASE", raising=False)
    from treeweft.application import indexer_service

    body = await indexer_service.health()

    assert body["release"] is None


@pytest.mark.asyncio
async def test_mcp_http_health_reports_versions(monkeypatch):
    monkeypatch.setenv("TREEWEFT_RELEASE", "2026.10.1")
    from treeweft.application import main

    body = await main.health()

    assert body == {"status": "ok", "version": versions.SOURCE_VERSION, "release": "2026.10.1"}


def test_openapi_documents_the_source_version():
    from treeweft.application.indexer_service import app

    assert app.openapi()["info"]["version"] == versions.SOURCE_VERSION
