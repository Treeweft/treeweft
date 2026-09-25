"""The publish workflow and Dockerfiles carry the ADR-004 release wiring."""
import fnmatch
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _workflow():
    return yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text())


def test_push_trigger_matches_only_calver_tags():
    # PyYAML parses the top-level `on:` key as the boolean True, not the string "on".
    wf = _workflow()
    on = wf.get("on", wf.get(True))
    tags = on["push"]["tags"]
    assert tags == ["v[0-9][0-9][0-9][0-9].*"]

    pattern = tags[0]
    assert fnmatch.fnmatchcase("v2026.10.1", pattern)
    assert fnmatch.fnmatchcase("v2026.10.1.1", pattern)
    assert not fnmatch.fnmatchcase("v1.0.0", pattern)
    assert not fnmatch.fnmatchcase("v10.2.3", pattern)


def test_check_tag_runs_the_script_with_full_tag_history():
    steps = _workflow()["jobs"]["check-tag"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert any("scripts/check_release_tags.py" in str(s.get("run", "")) for s in steps)


def test_build_passes_release_and_labels_source_version():
    steps = _workflow()["jobs"]["build"]["steps"]
    build = next(s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action"))
    assert "TREEWEFT_RELEASE=" in build["with"]["build-args"]
    meta = next(s for s in steps if str(s.get("uses", "")).startswith("docker/metadata-action"))
    assert "treeweft.source-version=" in meta["with"]["labels"]


def test_both_python_images_bake_the_release_and_ship_pyproject():
    for dockerfile in ("Dockerfile", "Dockerfile.indexer"):
        text = (ROOT / dockerfile).read_text()
        assert "ARG TREEWEFT_RELEASE" in text, dockerfile
        assert "ENV TREEWEFT_RELEASE=$TREEWEFT_RELEASE" in text, dockerfile
        assert "COPY pyproject.toml" in text, dockerfile  # versions.SOURCE_VERSION reads it
