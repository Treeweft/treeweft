# Data Model: Self-Provisioned Integration Services

This feature stores no data. Its entities are test-time configuration.

| Entity | Fields | Rules |
|---|---|---|
| Opt-in switch | `TREEWEFT_ITEST_CONTAINERS` | `1` enables provisioning; any other value or unset means off |
| Service definition | name (`postgres`, `milvus`, `neo4j`), image (from `docker-compose.yml`), exposed ports, wait strategy + startup timeout, generated credentials | Image resolved once per run; every port bound to `127.0.0.1` with a random host port |
| Service address | Postgres URL, Milvus URI, `Neo4jConn(uri, user, password)` | Explicit variable > container > skip (`resolve_mode`) |
| Failure logs | `.itest-logs/<service>.log` | Written only when the session had failures; git-ignored; uploaded by CI on failure |

State per service within a run: `unresolved → (explicit | starting → healthy | skipped) →
stopped`. A start or wait failure fails the run with the image name, the error and the recent logs.
