#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${MAA_WEB_VENV:-$ROOT_DIR/.venv}"
VENV_PY="$VENV_DIR/bin/python"
STAMP_FILE="$VENV_DIR/.maa-web-control-deps"

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "$PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return
  fi
  echo "Python 3.11+ is required but was not found." >&2
  exit 1
}

install_dependencies() {
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -e .
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP_FILE"
}

if [[ ! -x "$VENV_PY" ]]; then
  "$(find_python)" -m venv "$VENV_DIR"
fi

if [[ ! -f "$STAMP_FILE" || pyproject.toml -nt "$STAMP_FILE" ]]; then
  install_dependencies
fi

# 走飞牛统一网关时监听 unix socket：不占端口，鉴权由网关兜。
# socket 的权限不用在这里操心：uvicorn 绑定之后会自己 chmod 成 0666
# （uvicorn/config.py 的 bind_socket()），宿主上另一个 uid 的网关进程照样连得进来。
# 所以这里不放开 umask，反而要收紧 —— 否则 data/ 下的 notifications.json 之类
# （含 webhook 凭据）会被建成本机其他用户可读可写。
if [[ -n "${MAA_WEB_SOCKET:-}" ]]; then
  umask 077
  exec "$VENV_PY" -m uvicorn app.main:app --uds "${MAA_WEB_SOCKET}"
fi

exec "$VENV_PY" -m uvicorn app.main:app \
  --host "${MAA_WEB_HOST:-0.0.0.0}" \
  --port "${MAA_WEB_PORT:-8000}"
