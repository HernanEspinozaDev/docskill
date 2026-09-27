#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: Python 3 was not found (looked for '$PYTHON_BIN'). Install Python 3.10 or newer." >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: Python 3.10 or newer is required." >&2
    exit 1
fi

PANDOC_BIN="${PANDOC_BIN:-$(command -v pandoc || true)}"
if [ -z "$PANDOC_BIN" ] || [ ! -x "$PANDOC_BIN" ]; then
    cat >&2 <<'EOF'
ERROR: Pandoc 3.x was not found on PATH.
Install Pandoc 3.x with your system package manager or from the official
Pandoc installation instructions, then run setup_ubuntu.sh again.
EOF
    exit 1
fi

PANDOC_VERSION_LINE="$("$PANDOC_BIN" --version | head -n 1)"
if [[ ! "$PANDOC_VERSION_LINE" =~ ^pandoc[[:space:]]+([0-9]+)\. ]]; then
    echo "ERROR: Could not read Pandoc version from: $PANDOC_VERSION_LINE" >&2
    exit 1
fi
if [ "${BASH_REMATCH[1]}" -lt 3 ]; then
    echo "ERROR: Pandoc 3.x or newer is required; found: $PANDOC_VERSION_LINE" >&2
    exit 1
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    if ! "$PYTHON_BIN" -m venv --without-pip "$ROOT/.venv"; then
        echo "ERROR: Could not create .venv. On Ubuntu, install the matching python3-venv package." >&2
        exit 1
    fi
fi

if [ -e "$ROOT/.venv/bin/pandoc" ] && [ ! -L "$ROOT/.venv/bin/pandoc" ]; then
    echo "ERROR: $ROOT/.venv/bin/pandoc exists and is not a symlink; refusing to replace it." >&2
    exit 1
fi
ln -sfn "$PANDOC_BIN" "$ROOT/.venv/bin/pandoc"

# requirements.txt is comment-only: no Python packages need to be installed.
"$ROOT/.venv/bin/python" -c 'import sys; print("Environment Python:", sys.version.split()[0])'
echo "Pandoc: $("$ROOT/.venv/bin/pandoc" --version | head -n 1)"
echo "Python dependencies: none (standard library only)"
echo "Environment ready. Activate it with: source \"$ROOT/.venv/bin/activate\""
