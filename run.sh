#!/usr/bin/env bash
# myagent 启动器：优先用项目 venv（带本地推理运行时），没有就退回系统 python
set -e
cd "$(dirname "$0")"
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" -m entrypoints.cli "$@"
