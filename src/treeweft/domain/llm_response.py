"""LLM domain: what one chat response reports about itself — pure, no I/O.

Shared by the service's LLM adapter and the benchmark harness, so the two
agree on what "truncated" means without either importing the other.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The one definition of a reasoning block. The service adapter strips with this
# same pattern, so `ResponseSignals.empty` and what the adapter returns cannot
# drift apart.
THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# OpenAI-compatible endpoints report "length"; Anthropic's native API reports
# "max_tokens", or "model_context_window_exceeded" when the context window ran
# out first. Compared lower-cased. A reason not listed here reads as "not
# truncated", so add a provider's spelling when a new one is supported.
TRUNCATION_REASONS = frozenset(
    {"length", "max_tokens", "model_context_window_exceeded"}
)


def strip_thinking(text: str | None) -> str:
    """Remove <think>...</think> blocks left over from reasoning models."""
    return THINK_TAG_RE.sub("", text or "").strip()


def is_truncation(finish_reason: str | None) -> bool | None:
    """True if generation stopped at the token limit; None when unknown."""
    if not finish_reason or not isinstance(finish_reason, str):
        return None
    return finish_reason.lower() in TRUNCATION_REASONS


def _token_count(value) -> int | None:
    # bool is an int subclass; a True/False here is a malformed response.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class ResponseSignals:
    """Fields read from a chat-completions response body.

    A value the endpoint did not report is None — never 0, "" or a copy of
    the request — so "not reported" can't read as "nothing wrong". Holds no
    prompt or response text.
    """

    served_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    truncated: bool | None = None
    empty: bool | None = None

    @classmethod
    def from_response(cls, body) -> "ResponseSignals":
        """Never raises, whatever shape `body` has."""
        try:
            return cls._read(body)
        except Exception:
            return cls()

    @classmethod
    def _read(cls, body) -> "ResponseSignals":
        if not isinstance(body, dict):
            return cls()
        model = body.get("model")
        served_model = model if isinstance(model, str) and model else None

        usage = body.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = _token_count(usage.get("prompt_tokens"))
        output_tokens = _token_count(usage.get("completion_tokens"))

        finish_reason = None
        empty = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            reason = choice.get("finish_reason")
            finish_reason = reason if isinstance(reason, str) and reason else None
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            empty = not (isinstance(content, str) and strip_thinking(content))

        return cls(
            served_model=served_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            truncated=is_truncation(finish_reason),
            empty=empty,
        )


class ServedModelBaseline:
    """First-seen served model per requested model, for the process's life.

    Comparing served against *requested* would flag every call on a server
    that reports a file path, or a vendor that reports the dated id behind an
    alias. Comparing against what the endpoint itself said first flags only a
    change. A wrong model served from the very first call is therefore not a
    mismatch; both names are on the span for the operator to see.

    Two answers per call. *Mismatch* is the standing state: the served model
    is not the one this process started with, true on every call until it
    goes back. *Changed* is the moment: the served model differs from the
    previous call's, true once per swap. Alert on changed, chart mismatch.
    """

    def __init__(self) -> None:
        self._first_seen: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}

    def check(
        self, requested: str, served: str | None
    ) -> tuple[bool | None, bool | None]:
        """(mismatch, changed); both None when `served` was not reported, in
        which case nothing is remembered."""
        if not served:
            return None, None
        baseline = self._first_seen.setdefault(requested, served)
        previous = self._last_seen.get(requested, served)
        self._last_seen[requested] = served
        return served != baseline, served != previous

    def observe(self, requested: str, served: str | None) -> bool | None:
        """True if `served` differs from the baseline; None if not reported."""
        return self.check(requested, served)[0]
