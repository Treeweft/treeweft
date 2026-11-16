"""Credential state classification, scrubbing and the failure streak
(specs/005-backend-credential-preflight, FR-002, FR-007, FR-012)."""
import pytest

from treeweft.domain.credentials import (
    BackendAuthError,
    CredentialState,
    FailureStreak,
    classify,
    scrub,
)

V, A, U = CredentialState.VALID, CredentialState.AUTH_FAILED, CredentialState.UNREACHABLE


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, "", V),
        (204, "", V),
        (401, "Incorrect API key provided", A),
        (403, "forbidden", A),
        (403, "You do not have access to model gpt-9", U),
        (403, "quota exceeded for this project", U),
        (403, "not available in your region", U),
        (404, "", U),
        (400, "", U),
        (429, "rate limited", U),
        (500, "", U),
        (503, "", U),
        (None, "", U),  # timeout / DNS / TLS / connection refused
    ],
)
def test_classify(status, body, expected):
    assert classify(status, body) is expected


def test_scrub_removes_the_key_and_every_prefix_of_it():
    key = "sk-abcdefghijklmnop1234"  # gitleaks:allow (fake test key)
    text = f"bad key {key}; echoed prefix {key[:12]}***; short {key[:4]}"
    cleaned = scrub(text, [key])
    for length in range(4, len(key) + 1):
        assert key[:length] not in cleaned.replace("[redacted]", "")
    assert key not in cleaned


def test_scrub_removes_token_shaped_strings_even_for_unknown_keys():
    cleaned = scrub("Incorrect API key provided: sk-xzAjD*****************XyZ1.", [])
    assert "sk-xzAjD" not in cleaned
    assert "[redacted]" in cleaned


def test_scrub_leaves_ordinary_text_alone():
    assert scrub("embeddings backend api.openai.com is down", ["sk-secretvalue"]) == (
        "embeddings backend api.openai.com is down"
    )


def test_backend_auth_error_message_has_reason_code_and_no_body():
    exc = BackendAuthError("embedding", "api.openai.com", "text-embedding-3-small", 401)
    assert str(exc).startswith("embedding_auth_failed:")
    assert "api.openai.com" in str(exc)
    assert "HTTP 401" in str(exc)


def _err():
    return BackendAuthError("embedding", "h", "m", 401)


def test_streak_aborts_at_the_threshold():
    s = FailureStreak(3)
    for _ in range(2):
        s.record_failure(_err())
    assert not s.aborted
    s.record_failure(_err())
    assert s.aborted and s.consecutive == 3


def test_a_success_resets_the_count():
    s = FailureStreak(3)
    s.record_failure(_err())
    s.record_failure(_err())
    s.record_success()
    s.record_failure(_err())
    s.record_failure(_err())
    assert not s.aborted


def test_aborted_stays_aborted_after_a_late_success():
    s = FailureStreak(2)
    s.record_failure(_err())
    s.record_failure(_err())
    s.record_success()  # an in-flight file that finished after the abort
    assert s.aborted


def test_threshold_is_at_least_one():
    s = FailureStreak(0)
    s.record_failure(_err())
    assert s.aborted
