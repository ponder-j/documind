#!/usr/bin/env bash
# One-command deployment. Set DASHSCOPE_API_KEY only in the remote .env.
set -euo pipefail
REMOTE="${REMOTE:-server-4090}"
REMOTE_ROOT="${REMOTE_ROOT:-/workspace/team3/chatbot}"
SESSION="${CHATBOT_TMUX_SESSION:-chatbot-api}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[1/5] Preparing ${REMOTE}:${REMOTE_ROOT}"
ssh "$REMOTE" "mkdir -p '${REMOTE_ROOT}/scripts' '${REMOTE_ROOT}/materials'"

echo "[2/5] Uploading source code and labeled data"
rsync -az --delete --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  "${PROJECT_ROOT}/agent/" "${REMOTE}:${REMOTE_ROOT}/agent/"
rsync -az --delete \
  "${PROJECT_ROOT}/frontend/" "${REMOTE}:${REMOTE_ROOT}/frontend/"
rsync -az --delete "${PROJECT_ROOT}/scripts/init_db.py" "${REMOTE}:${REMOTE_ROOT}/scripts/init_db.py"
rsync -az "${PROJECT_ROOT}/requirements.txt" "${PROJECT_ROOT}/.env.example" "${REMOTE}:${REMOTE_ROOT}/"
if [[ -n "${REMOTE_ENV_FILE:-}" ]]; then
  [[ -f "${REMOTE_ENV_FILE}" ]] || { echo "REMOTE_ENV_FILE not found: ${REMOTE_ENV_FILE}" >&2; exit 1; }
  rsync -az "${REMOTE_ENV_FILE}" "${REMOTE}:${REMOTE_ROOT}/.env"
fi
if [[ -d "${PROJECT_ROOT}/materials/班级7用数据" ]]; then
  rsync -az --delete "${PROJECT_ROOT}/materials/班级7用数据/" "${REMOTE}:${REMOTE_ROOT}/materials/班级7用数据/"
fi

echo "[3/5] Creating uv environment and installing dependencies (bootstrapped from team3)"
ssh "$REMOTE" "REMOTE_ROOT='${REMOTE_ROOT}' bash -s" <<'REMOTE_SETUP'
set -euo pipefail
for CONDA_ROOT in /usr/local/miniconda3 /opt/conda "$HOME/miniconda3" "$HOME/anaconda3"; do
  [[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] && break
done
[[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] || { echo 'Conda installation not found' >&2; exit 1; }
source "$CONDA_ROOT/etc/profile.d/conda.sh"
conda activate team3
cd "$REMOTE_ROOT"
if ! command -v uv >/dev/null 2>&1; then
  python -m pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
uv venv --clear --python "$(command -v python)" .venv
uv pip install --python .venv/bin/python -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
[[ -f .env ]] || cp .env.example .env
REMOTE_SETUP

echo "[4/5] Initializing PostgreSQL data"
ssh "$REMOTE" "REMOTE_ROOT='${REMOTE_ROOT}' RESET_DB='${RESET_DB:-0}' INSTALL_POSTGRES='${INSTALL_POSTGRES:-1}' bash -s" <<'REMOTE_DB'
set -euo pipefail
for CONDA_ROOT in /usr/local/miniconda3 /opt/conda "$HOME/miniconda3" "$HOME/anaconda3"; do
  [[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] && break
done
[[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] || { echo 'Conda installation not found' >&2; exit 1; }
source "$CONDA_ROOT/etc/profile.d/conda.sh"
conda activate team3
cd "$REMOTE_ROOT"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
if [[ "${INSTALL_POSTGRES}" == "1" ]] && ! command -v pg_isready >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq postgresql postgresql-client
fi
if [[ "${INSTALL_POSTGRES}" == "1" ]]; then
  PG_VERSION="$(pg_lsclusters -h 2>/dev/null | awk 'NR==1 {print $1}')"
  PG_NAME="$(pg_lsclusters -h 2>/dev/null | awk 'NR==1 {print $2}')"
  if [[ -n "$PG_VERSION" && -n "$PG_NAME" ]]; then pg_ctlcluster "$PG_VERSION" "$PG_NAME" start >/dev/null 2>&1 || true; fi
  su - postgres -c "psql -v ON_ERROR_STOP=1 -c \"ALTER USER postgres PASSWORD 'postgres'\"" || true
  su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='chatbot'\" | grep -q 1 || createdb -O postgres chatbot" || true
fi
.venv/bin/python - <<'PY'
import os, sys
import psycopg
try:
    with psycopg.connect(os.getenv('DATABASE_URL', 'postgresql://postgres:postgres@127.0.0.1:5432/chatbot'), connect_timeout=3): pass
except Exception as exc:
    print('PostgreSQL is unavailable. Start PostgreSQL or set DATABASE_URL in the remote .env.', file=sys.stderr)
    print(f'Database check: {exc}', file=sys.stderr)
    raise SystemExit(2)
PY
if [[ "$RESET_DB" == "1" ]]; then .venv/bin/python scripts/init_db.py --reset; else .venv/bin/python scripts/init_db.py; fi
REMOTE_DB

echo "[5/5] Restarting FastAPI service in tmux session ${SESSION}"
ssh "$REMOTE" "REMOTE_ROOT='${REMOTE_ROOT}' SESSION='${SESSION}' bash -s" <<'REMOTE_RUN'
set -euo pipefail
for CONDA_ROOT in /usr/local/miniconda3 /opt/conda "$HOME/miniconda3" "$HOME/anaconda3"; do
  [[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] && break
done
[[ -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]] || { echo 'Conda installation not found' >&2; exit 1; }
source "$CONDA_ROOT/etc/profile.d/conda.sh"
conda activate team3
cd "$REMOTE_ROOT"
mkdir -p "${REMOTE_ROOT}/logs"
if tmux has-session -t "$SESSION" 2>/dev/null; then tmux kill-session -t "$SESSION"; fi
if [[ -f .env ]]; then set -a; source .env; set +a; fi
tmux new-session -d -s "$SESSION" "exec .venv/bin/python -m uvicorn agent.main:app --host 0.0.0.0 --port 8000 2>&1 | tee ${REMOTE_ROOT}/logs/chatbot.log"
for attempt in {1..15}; do
  curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  [[ "$attempt" == "15" ]] && { echo "FastAPI failed to start; inspect ${REMOTE_ROOT}/logs/chatbot.log" >&2; exit 1; }
  sleep 1
done
REMOTE_RUN
echo "Deployment complete. Use scripts/tunnel.sh for local access."
