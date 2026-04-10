#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

# If no args, launch the dashboard
if [ $# -eq 0 ]; then
  echo "Starting dashboard at http://localhost:5000"
  python dashboard.py
else
  python "$@"
fi
