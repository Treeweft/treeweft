#!/bin/sh
# Generate the SPA's runtime config before nginx starts.
#
# TREEWEFT_API_BASE must be reachable FROM THE BROWSER. The indexer runs on the
# host (published at localhost:8001), so this is a host URL, NOT an in-compose
# service name. The indexer's CORS allow-list (TREEWEFT_CORS_ORIGINS) must
# include this UI's origin for cross-origin calls to succeed.
set -eu
: "${TREEWEFT_API_BASE:=http://localhost:8001}"
cat > /usr/share/nginx/html/config.js <<EOF
window.__TREEWEFT_API_BASE__ = "${TREEWEFT_API_BASE}";
EOF
echo "treeweft-ui: wrote config.js (TREEWEFT_API_BASE=${TREEWEFT_API_BASE})"
