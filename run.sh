#!/usr/bin/env bash
# NICo Emulator (standalone) — http://127.0.0.1:9000
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -d .venv ] && PY=.venv/bin/python
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 9000 "$@"
