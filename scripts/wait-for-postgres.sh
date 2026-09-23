#!/bin/bash
# Block until the compose `postgres` service accepts TCP connections.
#
# Usage: scripts/wait-for-postgres.sh [docker compose args...]
#   e.g. scripts/wait-for-postgres.sh -f docker-compose.simple.yml
#
# The host indexer fails loud at startup if Postgres is unreachable, and on a
# fresh volume the container spends several seconds in initdb after
# `docker compose up -d` returns. Probe over TCP (-h 127.0.0.1), not the Unix
# socket: the entrypoint's temporary initdb server listens on the socket only,
# so a socket probe reports "ready" before the real server is up.
#
# POSTGRES_WAIT_SECONDS (default 60) bounds the wait.
set -euo pipefail

timeout="${POSTGRES_WAIT_SECONDS:-60}"
user="${POSTGRES_USER:-treeweft}"

echo "Waiting for Postgres (up to ${timeout}s)..."
for ((i = 0; i < timeout; i++)); do
    if docker compose "$@" exec -T postgres pg_isready -q -h 127.0.0.1 -U "$user" >/dev/null 2>&1; then
        echo "Postgres is ready."
        exit 0
    fi
    sleep 1
done

echo "ERROR: Postgres not ready after ${timeout}s. Check: docker compose $* logs postgres" >&2
exit 1
