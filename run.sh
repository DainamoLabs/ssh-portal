#!/usr/bin/env bash
# Start ssh-portal on any free local port and open it in your browser.
# Extra arguments go to ssh-portal, e.g.  ./run.sh --config ~/hosts.toml
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null; then
    echo "run.sh: uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

exec uv run ssh-portal --port 0 "$@"
