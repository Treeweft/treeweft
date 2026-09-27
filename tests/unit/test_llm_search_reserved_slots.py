"""LLM_SEARCH_RESERVED_SLOTS: LLM slots kept for searches (#22 follow-up).

Default 1, or 0 when LLM_CONCURRENCY is 1. An explicit value that leaves
chunk summaries no slot fails validate_config() naming the setting
(constitution V).
"""
import asyncio

import pytest

from treeweft.adapters.llm_api import llm_adapter
from treeweft.domain.priority_slots import Priority
from treeweft.infrastructure.config import ConfigError, llm_search_reserved_slots, validate_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_SEARCH_RESERVED_SLOTS", raising=False)
    monkeypatch.delenv("LLM_CONCURRENCY", raising=False)


class TestSetting:
    @pytest.mark.parametrize("concurrency,expected", [(1, 0), (2, 1), (4, 1), (16, 1)])
    def test_default(self, concurrency, expected):
        assert llm_search_reserved_slots(concurrency) == expected

    @pytest.mark.parametrize("raw,concurrency,expected", [("0", 4, 0), ("2", 4, 2), ("3", 4, 3), (" 1 ", 2, 1)])
    def test_explicit_value(self, monkeypatch, raw, concurrency, expected):
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", raw)
        assert llm_search_reserved_slots(concurrency) == expected

    @pytest.mark.parametrize("raw,concurrency", [("4", 4), ("1", 1), ("-1", 4), ("one", 4), ("1.5", 4)])
    def test_invalid_value_names_the_setting(self, monkeypatch, raw, concurrency):
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", raw)
        with pytest.raises(ConfigError, match="LLM_SEARCH_RESERVED_SLOTS"):
            llm_search_reserved_slots(concurrency)

    def test_blank_means_the_default(self, monkeypatch):
        """.env.example ships the setting empty."""
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", "")
        assert llm_search_reserved_slots(4) == 1

    def test_validate_config_fails_on_an_invalid_value(self, monkeypatch):
        monkeypatch.setattr("treeweft.infrastructure.config.required_settings", lambda: [])
        monkeypatch.setenv("LLM_CONCURRENCY", "2")
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", "2")
        with pytest.raises(ConfigError, match="LLM_SEARCH_RESERVED_SLOTS"):
            validate_config()

    def test_validate_config_accepts_the_default(self, monkeypatch):
        monkeypatch.setattr("treeweft.infrastructure.config.required_settings", lambda: [])
        validate_config()


@pytest.mark.asyncio
class TestWiring:
    async def test_summaries_leave_the_reserved_slot_to_searches(self, monkeypatch):
        monkeypatch.setattr(llm_adapter, "_slots", None)
        monkeypatch.setattr(llm_adapter, "LLM_CONCURRENCY", 4)
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", "1")

        slots = llm_adapter._get_slots()

        for _ in range(3):
            await asyncio.wait_for(slots.acquire(Priority.BACKGROUND), timeout=0.1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slots.acquire(Priority.BACKGROUND), timeout=0.01)
        await asyncio.wait_for(slots.acquire(Priority.INTERACTIVE), timeout=0.1)
        assert slots.in_use == 4

    async def test_zero_restores_the_old_behaviour(self, monkeypatch):
        monkeypatch.setattr(llm_adapter, "_slots", None)
        monkeypatch.setattr(llm_adapter, "LLM_CONCURRENCY", 4)
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", "0")

        slots = llm_adapter._get_slots()

        for _ in range(4):
            await asyncio.wait_for(slots.acquire(Priority.BACKGROUND), timeout=0.1)
        assert slots.in_use == 4


def test_docker_compose_passes_the_setting_to_the_indexer():
    """The compose indexer has no env_file: a setting it does not name in
    `environment:` never reaches the container, whatever `.env` says."""
    import pathlib

    import yaml

    compose = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    services = [s for s in compose["services"].values()
                if any(str(e).startswith("LLM_CONCURRENCY=") for e in s.get("environment") or [])]
    assert services, "no compose service passes LLM_CONCURRENCY"
    for service in services:
        names = {str(e).split("=", 1)[0] for e in service["environment"]}
        assert "LLM_SEARCH_RESERVED_SLOTS" in names
