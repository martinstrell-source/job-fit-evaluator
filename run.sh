#!/usr/bin/env bash
# Launch the job-fit-evaluator using ITS OWN .venv, regardless of which
# environment happens to be active in your shell. Avoids the "No module
# named 'openai'" error that comes from running with the wrong venv.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/streamlit run app.py "$@"
