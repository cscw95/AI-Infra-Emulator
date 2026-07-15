#!/usr/bin/env bash
# AI Infra Emulator — Vera Rubin NVL72 physical digital twin (:9100)
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -d .venv ] && PY=.venv/bin/python
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "${AI_INFRA_PORT:-9100}" "$@"
