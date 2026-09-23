"""Pre-rebrand TREELOOM_* env vars keep working as TREEWEFT_* fallbacks.

Existing .env files and shell profiles predate the Treeloom -> Treeweft
rename; `treeweft/__init__.py` copies each legacy var to its new name unless
the new name is already set, and warns once. See docs/migrating-from-treeloom.md.

Treeloom back-compat: remove on 2026-11-01 (#14).
"""
import warnings

import pytest

from treeweft import _alias_legacy_env


def test_legacy_var_alone_is_picked_up():
    env = {"TREELOOM_PROFILE": "simple"}
    with pytest.warns(FutureWarning, match="TREELOOM_PROFILE"):
        _alias_legacy_env(env)
    assert env["TREEWEFT_PROFILE"] == "simple"


def test_new_var_wins_when_both_are_set():
    env = {"TREELOOM_PROFILE": "simple", "TREEWEFT_PROFILE": "full"}
    with pytest.warns(FutureWarning):
        _alias_legacy_env(env)
    assert env["TREEWEFT_PROFILE"] == "full"


def test_empty_new_var_counts_as_unset():
    env = {"TREELOOM_MCP_API_KEY": "k", "TREEWEFT_MCP_API_KEY": ""}
    with pytest.warns(FutureWarning):
        _alias_legacy_env(env)
    assert env["TREEWEFT_MCP_API_KEY"] == "k"


def test_no_legacy_vars_is_silent_and_untouched():
    env = {"TREEWEFT_PROFILE": "full", "PATH": "/bin"}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _alias_legacy_env(env)
    assert env == {"TREEWEFT_PROFILE": "full", "PATH": "/bin"}


def test_warns_once_listing_every_legacy_name():
    env = {"TREELOOM_PROFILE": "simple", "TREELOOM_CACHE_DIR": "/c"}
    with pytest.warns(FutureWarning) as record:
        _alias_legacy_env(env)
    assert len(record) == 1
    msg = str(record[0].message)
    assert "TREELOOM_CACHE_DIR" in msg and "TREELOOM_PROFILE" in msg
    assert env["TREEWEFT_CACHE_DIR"] == "/c"


def test_already_aliased_names_do_not_warn_again():
    # __init__ runs the alias pass before and after loading .env; the second
    # pass must not re-warn about vars the first pass already migrated.
    env = {"TREELOOM_PROFILE": "simple"}
    with pytest.warns(FutureWarning):
        reported = _alias_legacy_env(env)
    env["TREELOOM_CACHE_DIR"] = "/c"  # e.g. only set in the old .env
    with pytest.warns(FutureWarning) as record:
        reported = _alias_legacy_env(env, reported)
    msg = str(record[0].message)
    assert "TREELOOM_CACHE_DIR" in msg and "TREELOOM_PROFILE" not in msg
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _alias_legacy_env(env, reported)
