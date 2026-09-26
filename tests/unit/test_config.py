"""Index-stamp check tunables (ADR-004 §3) and prompt-pin tunables (ADR-003):
defaults, overrides, and that a malformed value fails validate_config() with a
message naming the setting (constitution V), instead of surfacing later as a
runtime crash."""

import pytest

from treeweft.infrastructure.config import (
    ConfigError,
    index_status_refresh_seconds,
    index_verify_interval_seconds,
    index_verify_timeout_seconds,
    prompt_pins_refresh_seconds,
    validate_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "INDEX_VERIFY_INTERVAL_SECONDS",
        "INDEX_VERIFY_TIMEOUT_SECONDS",
        "INDEX_STATUS_REFRESH_SECONDS",
        "PROMPT_PINS_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_verify_interval_default(self):
        assert index_verify_interval_seconds() == 60

    def test_verify_timeout_default(self):
        assert index_verify_timeout_seconds() == 15

    def test_status_refresh_default(self):
        assert index_status_refresh_seconds() == 5

    def test_pins_refresh_default(self):
        assert prompt_pins_refresh_seconds() == 5


class TestOverride:
    def test_verify_interval_override(self, monkeypatch):
        monkeypatch.setenv("INDEX_VERIFY_INTERVAL_SECONDS", "30")
        assert index_verify_interval_seconds() == 30

    def test_verify_timeout_override(self, monkeypatch):
        monkeypatch.setenv("INDEX_VERIFY_TIMEOUT_SECONDS", "5.5")
        assert index_verify_timeout_seconds() == 5.5

    def test_status_refresh_override(self, monkeypatch):
        monkeypatch.setenv("INDEX_STATUS_REFRESH_SECONDS", "10")
        assert index_status_refresh_seconds() == 10

    def test_pins_refresh_override(self, monkeypatch):
        monkeypatch.setenv("PROMPT_PINS_REFRESH_SECONDS", "10")
        assert prompt_pins_refresh_seconds() == 10


class TestInvalidValuesRejected:
    @pytest.mark.parametrize(
        "name,getter",
        [
            ("INDEX_VERIFY_INTERVAL_SECONDS", index_verify_interval_seconds),
            ("INDEX_VERIFY_TIMEOUT_SECONDS", index_verify_timeout_seconds),
            ("INDEX_STATUS_REFRESH_SECONDS", index_status_refresh_seconds),
            ("PROMPT_PINS_REFRESH_SECONDS", prompt_pins_refresh_seconds),
        ],
    )
    def test_non_numeric_value_rejected(self, monkeypatch, name, getter):
        monkeypatch.setenv(name, "not-a-number")
        with pytest.raises(ConfigError, match=name):
            getter()

    @pytest.mark.parametrize(
        "name,getter",
        [
            ("INDEX_VERIFY_INTERVAL_SECONDS", index_verify_interval_seconds),
            ("INDEX_VERIFY_TIMEOUT_SECONDS", index_verify_timeout_seconds),
            ("INDEX_STATUS_REFRESH_SECONDS", index_status_refresh_seconds),
            ("PROMPT_PINS_REFRESH_SECONDS", prompt_pins_refresh_seconds),
        ],
    )
    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_non_positive_value_rejected(self, monkeypatch, name, getter, bad):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ConfigError, match=name):
            getter()


class TestValidateConfigChecksThem:
    def test_validate_config_raises_on_bad_verify_interval(self, monkeypatch):
        # Satisfy the base required settings so only our new check is exercised.
        monkeypatch.setenv("EMBEDDING_MODEL", "x")
        monkeypatch.setenv("VECTOR_DIM", "8")
        monkeypatch.setenv("LLM_URL", "http://x")
        monkeypatch.setenv("LLM_MODEL", "x")
        monkeypatch.setenv("VECTOR_STORE", "lancedb")
        monkeypatch.setenv("GRAPH_STORE", "sqlite")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "tei")
        monkeypatch.setenv("EMBEDDING_URL", "http://x")
        monkeypatch.setenv("RERANKER_PROVIDER", "local-st")
        monkeypatch.setenv("INDEX_VERIFY_INTERVAL_SECONDS", "-5")
        with pytest.raises(ConfigError, match="INDEX_VERIFY_INTERVAL_SECONDS"):
            validate_config()

    def test_validate_config_raises_on_bad_pins_refresh(self, monkeypatch):
        # Satisfy the base required settings so only our new check is exercised.
        monkeypatch.setenv("EMBEDDING_MODEL", "x")
        monkeypatch.setenv("VECTOR_DIM", "8")
        monkeypatch.setenv("LLM_URL", "http://x")
        monkeypatch.setenv("LLM_MODEL", "x")
        monkeypatch.setenv("VECTOR_STORE", "lancedb")
        monkeypatch.setenv("GRAPH_STORE", "sqlite")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "tei")
        monkeypatch.setenv("EMBEDDING_URL", "http://x")
        monkeypatch.setenv("RERANKER_PROVIDER", "local-st")
        monkeypatch.setenv("PROMPT_PINS_REFRESH_SECONDS", "-5")
        with pytest.raises(ConfigError, match="PROMPT_PINS_REFRESH_SECONDS"):
            validate_config()
