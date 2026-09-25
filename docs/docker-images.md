# Docker images

Treeweft publishes its container images to Docker Hub under the `treeweft`
namespace. This page covers what is published, how to consume the images, and
how a maintainer cuts a release.

## Published images

| Image | Built from | Platforms | Role |
|---|---|---|---|
| `treeweft/indexer` | `Dockerfile.indexer` | `linux/amd64` | The indexer FastAPI service (`treeweft.indexer_service:app`, port 8001). Ships the full dependency set and a pre-downloaded tiktoken cache so it runs without egress. |
| `treeweft/mcp-server` | `Dockerfile` | `linux/amd64`, `linux/arm64` | The deprecated HTTP+SSE MCP transport (`treeweft.main:app`, port 8000). Pure HTTP proxy to an indexer; opt-in via the `http-mcp` compose profile. The default stdio transport (`treeweft-mcp`) needs no container. |
| `treeweft/ui` | `ui/Dockerfile` | `linux/amd64`, `linux/arm64` | The operator SPA served by nginx on port 80. The API base is injected at container start from `TREEWEFT_API_BASE`. |
| `treeweft/qwen3-reranker` | `services/qwen3_reranker/Dockerfile` | `linux/amd64` | GPU reranker service for `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` (TEI cannot host Qwen3 rerankers). CUDA 12.1 base; needs the NVIDIA container runtime. |

Not published: `services/jina_reranker` (an experimental alternative reranker,
same CUDA base) and the embedding/reranking TEI services, Postgres, Neo4j,
Milvus and the observability stack, which are upstream images referenced
directly by `docker-compose.yml`.

The indexer image is `amd64` only because its dependency set (pymilvus,
tree-sitter grammars, chonkie) is slow to build under QEMU emulation. The
indexer is normally run on the host anyway (it must read arbitrary user paths;
see the Quick Start), so the image matters mostly for server deployments.

## Versioning

Treeweft has two version lines (ADR-004):

- **Product releases are CalVer.** Published images are named for the date the
  release was cut: `YYYY.M.D` without zero padding (`2026.10.1`), with a
  counter for a second release the same day (`2026.10.1.1`). The rolling
  month tag (`2026.10`) and `latest` point at the newest release.
- **The source is SemVer.** `pyproject.toml` carries `MAJOR.MINOR.PATCH`
  (from `1.0.0`). MAJOR means a breaking change to the indexer HTTP API, the
  MCP tools or the index schema; MINOR means additions; PATCH means fixes.
  `tests/unit/test_contracts.py` enforces the bump against the snapshots in
  `contracts/`.
- **A feature PR that changes the API or MCP tool surface bumps
  `pyproject.toml` itself.** `tests/unit/test_contracts.py` fails until it
  does, naming the minimum version required. The release PR does not decide
  the version; it only regenerates the contract snapshots (see "Cutting a
  release" below) and sets the version itself only when no feature PR
  already bumped it.
- **Every release commit carries both tags:** `v<SemVer>` first, then
  `v<CalVer>`, which triggers the publish workflow. The workflow's `check-tag`
  job (`scripts/check_release_tags.py`) refuses a malformed CalVer tag or a
  missing SemVer tag.
- **The month tag is a stability pin.** A release that bumps the SemVer MAJOR
  is only ever the first release of its month, and `check-tag` refuses one
  mid-month. Pinning `TREEWEFT_IMAGE_TAG=2026.10` therefore never pulls in a
  breaking change. Releases from before 1.0.0 (CalVer source versions) are
  outside this rule.
- `GET /health` reports both: `"version"` (SemVer) and `"release"` (CalVer,
  `null` when running from a source checkout).

## Tags

| Trigger | Tags pushed |
|---|---|
| Git tag `vYYYY.M.D[.N]` | `YYYY.M.D[.N]`, `YYYY.M` (rolling month), `latest` |
| Manual workflow run (any branch) | `edge`, `sha-<short commit>` |
| Pull request touching a Dockerfile | build only, nothing pushed; amd64 only, reranker skipped, superseded by a newer push to the PR |

`latest` always tracks the most recent release tag, never `main`. The
rolling month tag (`2026.9`) moves to the newest release within that month.

## Using the published images

Both compose files declare an `image:` next to each `build:`, so a checkout can
either build locally (the default, what `run.sh` does) or pull the published
images:

```bash
# Pull the release images instead of building
docker compose pull ui mcp-server qwen3-reranker
docker compose up -d ui

# Pin a specific release (or a month: TREEWEFT_IMAGE_TAG=2026.9)
TREEWEFT_IMAGE_TAG=2026.9.22 docker compose pull ui
```

`TREEWEFT_IMAGE_NAMESPACE` (default `treeweft`) and `TREEWEFT_IMAGE_TAG`
(default `latest`) select the registry namespace and tag for every Treeweft
image at once. Running `docker compose build` still works and tags the local
build with the same name, which is why a subsequent `up` uses whichever you
did last; pass `--pull` to `up` to force the registry copy.

Running the indexer from its image outside compose:

```bash
docker run --rm -p 8001:8001 --env-file .env \
  -v /path/to/repos:/data/repos:ro \
  treeweft/indexer:latest
```

Remember the container cannot see host paths unless they are mounted, and
service URLs in `.env` that point at `localhost` must be rewritten to
addresses reachable from inside the container.

## Releasing (maintainers)

The publish workflow is `.github/workflows/docker-publish.yml`. It needs two
repository secrets and accepts one optional variable, all set under the
GitHub repository's Settings → Secrets and variables → Actions:

| Name | Kind | Value |
|---|---|---|
| `DOCKERHUB_USERNAME` | secret | The Docker Hub login that owns (or is a member of) the namespace |
| `DOCKERHUB_TOKEN` | secret | A Docker Hub access token with Read & Write scope (Account settings → Personal access tokens). Never a password. |
| `DOCKERHUB_NAMESPACE` | variable | Optional. Defaults to `treeweft`; set it if the account name differs. |

Equivalent CLI, run by whoever holds the token:

```bash
gh secret set DOCKERHUB_USERNAME --repo treeweft/treeweft
gh secret set DOCKERHUB_TOKEN --repo treeweft/treeweft
gh variable set DOCKERHUB_NAMESPACE --repo treeweft/treeweft --body treeweft
```

Cutting a release:

1. **Release PR.** A feature PR that changed the API or MCP tool surface
   already bumped `version` in `pyproject.toml` — `tests/unit/test_contracts.py`
   fails until it does, naming the minimum version. The release PR sets the
   version itself only if no feature PR already did. Either way, run `uv lock`
   and `python scripts/update_contracts.py` to record the new contract
   snapshots, and move the `## Unreleased` entries in `CHANGELOG.md` under a
   new `## <SemVer> — <CalVer>` heading. A release with a MAJOR bump must be
   the first release of its month.
2. **Merge**, then tag the merge commit twice, SemVer first:

       git tag v1.2.0 && git push origin v1.2.0
       git tag v2026.10.1 && git push origin v2026.10.1

3. Watch the "Docker images" workflow. `check-tag` runs first; then four build
   jobs push `2026.10.1`, `2026.10` and `latest`.

A pre-release smoke build without touching `latest`: run the workflow manually
from the Actions tab (or `gh workflow run docker-publish.yml --ref <branch>`).
That pushes `edge` and `sha-<commit>` only.

Docker Hub repositories are created automatically on first push. To make them
public they must be public on Docker Hub; a free account defaults new
repositories to public.

## Building locally

The published names are the same ones `docker compose build` produces, so a
local build can be pushed by hand when needed:

```bash
docker login
docker compose build indexer ui mcp-server
docker push treeweft/ui:latest
```

Prefer the workflow: it applies the OCI labels, builds multi-arch manifests
for the ui and MCP images, and never has a developer's `.env` in scope.
