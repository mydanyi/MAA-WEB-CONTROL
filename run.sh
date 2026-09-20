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
# socket 的权限和属主交给 serve_socket.py 定（0600 + package 用户）——
# 不能走 uvicorn 的 --uds，它会无条件 chmod 0666，配合本机可穿越的父目录，
# 等于任何本地用户都能绕过网关直连 API。理由和实测数据见那个文件的注释。
# 这里仍然收紧 umask：data/ 下的 notifications.json 之类（含 webhook 凭据）
# 不能被建成本机其他用户可读可写。
if [[ -n "${MAA_WEB_SOCKET:-}" ]]; then
  umask 077
  exec "$VENV_PY" "$ROOT_DIR/serve_socket.py"
fi

exec "$VENV_PY" -m uvicorn app.main:app \
  --host "${MAA_WEB_HOST:-0.0.0.0}" \
  --port "${MAA_WEB_PORT:-8000}"
