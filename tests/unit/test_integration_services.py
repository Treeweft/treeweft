"""Unit tests for tests/integration/_services.py helpers (no Docker required)."""

import pytest

from tests.integration._services import (
    SERVICE_VARS,
    SWITCH,
    apply_startup_timeout,
    bind_loopback,
    compose_image,
    resolve_mode,
    skip_reason,
)


class TestComposeImage:
    def test_postgres_image(self):
        assert compose_image("postgres") == "pgvector/pgvector:pg16"

    def test_milvus_image(self):
        assert compose_image("milvus") == "milvusdb/milvus:v2.5.4"

    def test_neo4j_image(self):
        assert compose_image("neo4j") == "neo4j:5-community"

    def test_missing_service_raises_naming_it(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "services:\n"
            "  postgres:\n"
            "    image: pgvector/pgvector:pg16\n"
        )
        with pytest.raises(ValueError, match="milvus"):
            compose_image("milvus", compose_path=compose_path)

    def test_missing_service_error_names_compose_path(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text("services:\n  postgres:\n    image: pgvector/pgvector:pg16\n")
        with pytest.raises(ValueError, match=str(compose_path).replace("\\", "\\\\")):
            compose_image("milvus", compose_path=compose_path)

    def test_missing_image_key_raises_naming_service(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "services:\n"
            "  neo4j:\n"
            "    ports:\n"
            "      - '7687:7687'\n"
        )
        with pytest.raises(ValueError, match="neo4j"):
            compose_image("neo4j", compose_path=compose_path)


class TestBindLoopback:
    def test_binds_every_port_to_loopback_random(self):
        result = bind_loopback({5432: None, 9091: None})
        assert result == {5432: ("127.0.0.1", None), 9091: ("127.0.0.1", None)}

    def test_returns_new_dict(self):
        original = {5432: None}
        result = bind_loopback(original)
        assert result is not original
        assert original == {5432: None}

    def test_empty_ports(self):
        assert bind_loopback({}) == {}


class TestResolveMode:
    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_explicit_wins_over_switch_on(self, service):
        var = SERVICE_VARS[service]
        env = {var: "postgresql://x", SWITCH: "1"}
        assert resolve_mode(service, env) == "explicit"

    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_explicit_wins_over_switch_off(self, service):
        var = SERVICE_VARS[service]
        env = {var: "postgresql://x", SWITCH: "0"}
        assert resolve_mode(service, env) == "explicit"

    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_switch_without_variable_gives_container(self, service):
        env = {SWITCH: "1"}
        assert resolve_mode(service, env) == "container"

    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_neither_gives_skip(self, service):
        assert resolve_mode(service, {}) == "skip"

    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_empty_explicit_var_is_ignored(self, service):
        var = SERVICE_VARS[service]
        env = {var: "", SWITCH: "1"}
        assert resolve_mode(service, env) == "container"

    @pytest.mark.parametrize("service", list(SERVICE_VARS))
    def test_empty_explicit_var_and_no_switch_is_skip(self, service):
        var = SERVICE_VARS[service]
        env = {var: ""}
        assert resolve_mode(service, env) == "skip"

    @pytest.mark.parametrize("switch_value", ["true", "yes", "2", "on", ""])
    def test_only_literal_one_counts_as_on(self, switch_value):
        env = {SWITCH: switch_value}
        assert resolve_mode("postgres", env) == "skip"


class TestSkipReason:
    def test_postgres_names_both_options(self):
        reason = skip_reason("postgres")
        assert "POSTGRES_TEST_URL" in reason
        assert f"{SWITCH}=1" in reason

    def test_milvus_names_both_options(self):
        reason = skip_reason("milvus")
        assert "MILVUS_TEST_URI" in reason
        assert f"{SWITCH}=1" in reason

    def test_neo4j_names_both_options(self):
        reason = skip_reason("neo4j")
        assert "NEO4J_TEST_URI" in reason
        assert f"{SWITCH}=1" in reason


class _FakeStrategy:
    def __init__(self):
        self.timeout = 120.0

    def with_startup_timeout(self, seconds):
        self.timeout = seconds
        return self


class _FakeContainer:
    def __init__(self, strategy=None):
        self._wait_strategy = strategy


class TestApplyStartupTimeout:
    def test_sets_the_timeout_on_a_strategy_built_in_the_constructor(self):
        """MilvusContainer builds its wait strategy in __init__, which reads
        the library's global timeout then; the per-start override is too late."""
        strategy = _FakeStrategy()

        apply_startup_timeout(_FakeContainer(strategy), 180)

        assert strategy.timeout == 180

    def test_container_without_a_strategy_is_left_alone(self):
        """Postgres and Neo4j build theirs inside start(), after the global
        override is in place."""
        container = _FakeContainer()

        apply_startup_timeout(container, 60)

        assert container._wait_strategy is None
