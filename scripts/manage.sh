#!/usr/bin/env bash
# ============================================================================
# team3 企业文档智能体 —— 一键启动/守护脚本
#
# 管理两个常驻服务（均通过 tmux 保持后台运行，SSH 断开不退出）：
#   * vl      : LLaMA-Factory 部署的 InternVL3-2B 微调模型（OpenAI 兼容 API）
#               tmux 会话 vl-api       端口 5003   模型名 vl
#   * chatbot : 对话 Agent + FastAPI 服务（agent.main:app）
#               tmux 会话 chatbot-api  端口 8000
#
# 用法（在本机或服务器上均可直接执行，本机执行会自动 ssh 到 server-4090）：
#   ./scripts/manage.sh start                # 启动全部
#   ./scripts/manage.sh start vl             # 只启动 InternVL 识图 API
#   ./scripts/manage.sh start chatbot        # 只启动 Chatbot
#   ./scripts/manage.sh stop   [all|vl|chatbot]
#   ./scripts/manage.sh restart [all|vl|chatbot]
#   ./scripts/manage.sh status
#   ./scripts/manage.sh logs   [vl|chatbot]  # 跟踪日志（Ctrl-C 退出）
#
# 日志目录：/workspace/team3/logs/
# ============================================================================
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-server-4090}"
CHATBOT_ROOT="${CHATBOT_ROOT:-/workspace/team3/chatbot}"
ACTION="${1:-start}"
TARGET="${2:-all}"
# 仅在直接执行（非 bash -s 远程管道）时需要 SELF；远程经 stdin 执行时 BASH_SOURCE 为空。
SELF=""
if [[ ${#BASH_SOURCE[@]} -gt 0 ]]; then
  SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
fi

usage() {
  echo "用法: $0 {start|stop|restart|status|logs} [all|vl|chatbot]" >&2
  exit 2
}
case "$ACTION" in start|stop|restart|status|logs) ;; *) usage ;; esac
case "$TARGET" in all|vl|chatbot) ;; *) usage ;; esac

# ---- 在本机（非服务器）执行时，自动通过 SSH 到服务器上运行本脚本 ----
if [[ ! -d "$CHATBOT_ROOT" ]]; then
  echo "[manage] 本机未发现 ${CHATBOT_ROOT}，自动经 ssh ${REMOTE_HOST} 执行: ${ACTION} ${TARGET}"
  exec ssh "$REMOTE_HOST" "CHATBOT_ROOT='${CHATBOT_ROOT}' bash -s ${ACTION} ${TARGET}" < "$SELF"
fi

# ===================== 以下代码在服务器上执行 =====================
# 需要时加载 conda（llamafactory-cli 用绝对路径调用，此处仅为兼容交互环境）
if [[ -f /usr/local/miniconda3/etc/profile.d/conda.sh ]]; then
  # shellcheck disable=SC1091
  source /usr/local/miniconda3/etc/profile.d/conda.sh
  conda activate team3 2>/dev/null || true
fi

LLAMAFACTORY_CLI=/usr/local/miniconda3/envs/team3/bin/llamafactory-cli
CHATBOT_PY="${CHATBOT_ROOT}/.venv/bin/python"
MODEL_PATH=/workspace/team3/models/my_best_model
LOG_DIR=/workspace/team3/logs
mkdir -p "${LOG_DIR}" "${CHATBOT_ROOT}/data/uploads"

SESSION_VL=vl-api
SESSION_CHATBOT=chatbot-api
VL_URL=http://127.0.0.1:5003/v1/models
CHATBOT_URL=http://127.0.0.1:8000/health
VL_LOG="${LOG_DIR}/vl-api.log"
CHATBOT_LOG="${LOG_DIR}/chatbot.log"

have() { command -v "$1" >/dev/null 2>&1; }
tmux_has() { tmux has-session -t "$1" 2>/dev/null; }

precheck() {
  local fail=0
  [[ -x "$LLAMAFACTORY_CLI" ]] || { echo "[vl] 缺少 ${LLAMAFACTORY_CLI}（请确认 team3 conda 环境）" >&2; fail=1; }
  [[ -d "$MODEL_PATH" ]] || { echo "[vl] 模型目录不存在: ${MODEL_PATH}" >&2; fail=1; }
  [[ -x "$CHATBOT_PY" ]] || { echo "[chatbot] 缺少虚拟环境 ${CHATBOT_PY}（先运行 scripts/deploy_remote.sh）" >&2; fail=1; }
  have tmux || { echo "[manage] 缺少 tmux" >&2; fail=1; }
  have curl || { echo "[manage] 缺少 curl" >&2; fail=1; }
  (( fail )) && exit 1
  return 0
}

start_vl() {
  if tmux_has "$SESSION_VL"; then
    echo "[vl] 已在运行（tmux: ${SESSION_VL}）"
    return 0
  fi
  echo "[vl] 启动 InternVL 识图服务 → ${VL_LOG}"
  tmux new-session -d -s "$SESSION_VL" \
    "export API_PORT=5003 API_MODEL_NAME=vl HF_ENDPOINT=https://hf-mirror.com; \
exec ${LLAMAFACTORY_CLI} api --model_name_or_path ${MODEL_PATH} --template intern_vl \
--infer_backend huggingface --trust_remote_code True 2>&1 | tee -a ${VL_LOG}"
  for i in $(seq 1 120); do
    if curl -fsS -m 3 "$VL_URL" 2>/dev/null | grep -q '"id":"vl"'; then
      echo "[vl] 就绪（等待 ${i} 次探测）"
      return 0
    fi
    sleep 2
  done
  echo "[vl] 启动超时，请查看 ${VL_LOG}" >&2
  return 1
}

start_chatbot() {
  if tmux_has "$SESSION_CHATBOT"; then
    echo "[chatbot] 已在运行（tmux: ${SESSION_CHATBOT}）"
    return 0
  fi
  if [[ -f "${CHATBOT_ROOT}/.env" ]]; then
    set -a; # shellcheck disable=SC1091
    source "${CHATBOT_ROOT}/.env"
    set +a
  else
    echo "[chatbot] 警告: ${CHATBOT_ROOT}/.env 不存在，使用代码内默认配置（DASHSCOPE_API_KEY 需自行 export）" >&2
  fi
  echo "[chatbot] 启动 Chatbot → ${CHATBOT_LOG}"
  cd "$CHATBOT_ROOT"
  tmux new-session -d -s "$SESSION_CHATBOT" -c "$CHATBOT_ROOT" \
    "exec ${CHATBOT_PY} -m uvicorn agent.main:app --host 0.0.0.0 --port 8000 2>&1 | tee -a ${CHATBOT_LOG}"
  for i in $(seq 1 60); do
    if curl -fsS -m 3 "$CHATBOT_URL" >/dev/null 2>&1; then
      echo "[chatbot] 就绪（等待 ${i} 次探测）"
      return 0
    fi
    sleep 1
  done
  echo "[chatbot] 启动超时，请查看 ${CHATBOT_LOG}" >&2
  return 1
}

stop_vl() {
  if tmux_has "$SESSION_VL"; then
    tmux kill-session -t "$SESSION_VL"
    echo "[vl] 已停止"
  else
    echo "[vl] 未在运行"
  fi
}

stop_chatbot() {
  if tmux_has "$SESSION_CHATBOT"; then
    tmux kill-session -t "$SESSION_CHATBOT"
    echo "[chatbot] 已停止"
  else
    echo "[chatbot] 未在运行"
  fi
}

status() {
  echo "== tmux 会话 =="
  tmux ls 2>/dev/null | grep -E "^(vl-api|chatbot-api):" || echo "（无）"
  echo
  echo "== InternVL vl (5003) =="
  if curl -fsS -m 3 "$VL_URL" 2>/dev/null; then
    echo
    echo "[vl] 运行中"
  else
    echo "[vl] 未运行/未就绪"
  fi
  echo
  echo "== Chatbot (8000) =="
  if curl -fsS -m 3 "$CHATBOT_URL" 2>/dev/null; then
    echo
    echo "[chatbot] 运行中"
  else
    echo "[chatbot] 未运行/未就绪"
  fi
  echo
  echo "== 日志 =="
  echo "  vl:      ${VL_LOG}"
  echo "  chatbot: ${CHATBOT_LOG}"
}

case "$ACTION" in
  start)
    precheck
    case "$TARGET" in all) start_vl; start_chatbot ;; vl) start_vl ;; chatbot) start_chatbot ;; esac
    ;;
  stop)
    case "$TARGET" in
      all) stop_vl; stop_chatbot ;;
      vl) stop_vl ;;
      chatbot) stop_chatbot ;;
    esac
    ;;
  restart)
    case "$TARGET" in
      all) stop_vl; stop_chatbot ;;
      vl) stop_vl ;;
      chatbot) stop_chatbot ;;
    esac
    precheck
    case "$TARGET" in all) start_vl; start_chatbot ;; vl) start_vl ;; chatbot) start_chatbot ;; esac
    ;;
  status) status ;;
  logs)
    case "$TARGET" in
      vl) exec tail -f "$VL_LOG" ;;
      chatbot) exec tail -f "$CHATBOT_LOG" ;;
      all) echo "请指定 vl 或 chatbot" >&2; exit 2 ;;
    esac
    ;;
esac
