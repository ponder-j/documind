# 企业文档处理智能体：实现状态与计划

更新时间：2026-09-01

## 已实现

### 服务与前端

- FastAPI 服务入口：`agent/main.py`、`agent/api.py`（路由层；核心逻辑拆分到 `agent/` 下 config/state/db/intent/tools/importers/vision/agents 模块）。
- 原生 HTML/CSS/JavaScript 对话页面：`frontend/index.html`。
- `GET /health` 健康检查和首页访问。
- `POST /api/chat` 统一处理文本问题与 PNG 发注书上传。
- 会话 ID、客户端请求 ID、trace ID，以及内存会话历史。
- 同一 `conversation_id` 会保存 Qwen 对话历史、上一轮订单结果和待确认单据；上传识别后前端展示可编辑识别结果卡片，可直接点击“确认导入”或继续对话分析。
- 图片发送后前端会立即重置文件控件和文件名显示，下一轮对话不会意外重复提交上一张图片。
- CORS、上传文件 MIME/大小校验（仅 PNG，默认不超过 10 MB）和服务器落盘。

### 单据识别与导入

- 已接入 DashScope 兼容 API 的 Qwen3.8-Flash 文本对话。
- 已接入 Qwen 多模态图片识别，要求模型返回结构化 JSON。
- 已实现对 Markdown 代码块、思考文本和额外说明的 JSON 容错解析。
- Mock 模式可在模型服务不可用时用于前端联调。
- `POST /api/documents/import` 支持用户确认后的明细幂等写入 PostgreSQL `orders` 表，通过 `client_request_id` 去重。
- 导入不再经过模型：识别完成后前端渲染可编辑的结构化数据卡片，用户核对/修改后点击“确认导入”按钮，前端直接提交当前表单数据到 `POST /api/documents/import`。
- 前端可修改顾客公司、发注日、源公司、项目、数量和单价，并支持添加/删除订单明细；输入框是唯一数据源，修改后的数据在点击导入时同步提交。
- 后端 `persist_import` 统一清洗数量/单价（去除逗号、全角符号、`¥` 等并转换为非负浮点数），发注日校验为 `YYYY-MM-DD`；非法数据返回 HTTP 400 与可读错误，不再出现 500 后端的二次解析问题。
- 已从模型工具白名单移除 `import_pending_document`，对话中“导入数据库”只提示使用页面按钮，模型无法调用或模拟导入。
- 前端 `responseJson` 先检查 `Content-Type` 再解析 JSON，服务端返回非 JSON/500 时展示可读错误，不再触发 `JSON.parse` 报错。
- `scripts/init_db.py` 可创建订单表、索引，并将 `output_label.xlsx` 导入数据库；数量、单价和销售金额会做基础转换。

### 订单问答

- 已实现基于规则的意图识别：订单查询、数量/金额聚合、年度趋势和普通对话。
- 已实现 PostgreSQL 参数化查询：按顾客公司、年份筛选订单。
- 公司名解析会先移除“查询/统计/分析”等动作词，避免将整句请求误当作客户名；并支持通过对话查询数据库状态（连接、表、记录数、客户数和日期范围）。
- 订单明细查询不再设置 200 条业务上限，SQL/API 返回全部匹配记录；Qwen 工具消息只发送少量预览行，完整记录保留在服务端供 Python 统计。
- Qwen 工具调用已接入：模型可选择订单查询、销售聚合、年度趋势、数据库状态和 Python 最高/最低/平均统计；多轮追问会复用上一轮完整查询结果。
- 已实现订单数量、销售数量、销售金额聚合。
- 已实现按年度汇总销售数量和金额的趋势数据。
- 已新增 `company_ranking` 工具：由 Qwen 识别公司/客户维度的排名意图，再按公司聚合总交易额或销售数量并返回前 N 名。
- Python 统计工具的最大/最小金额比较已增加显式数值键，避免订单金额相同时比较字典导致异常回退。
- 普通聊天请求可转发给 Qwen；数据库问答无匹配时会返回无记录提示。

### 部署

- `scripts/deploy_remote.sh`：通过 SSH 在远程 `server-4090` 的 `team3` Conda 环境部署、初始化数据库并用 tmux 启动 8000 端口服务。
- 部署脚本会在远程项目目录创建 `.venv` uv 环境，使用清华源安装 `requirements.txt`，FastAPI 由 `.venv/bin/python` 启动。
- `scripts/tunnel.sh`：建立本地到远程 8000/5001 端口的 SSH 转发。
- API Key 仅从远程环境变量 `DASHSCOPE_API_KEY` 读取，不写入代码。

## 计划实现

1. **数据与存储完善**：验证并记录全量 xlsx 导入结果；补充 `image_path`、原始数据、异常记录和更完整的数据统计。
2. **意图与对话编排**：增加模型辅助意图识别、参数校验、澄清问题、上下文追问，以及 `compare_periods`、`follow_up` 等多跳任务。
3. **工具模块化**：将查询、聚合、趋势和实体归一化拆成独立工具/Schema，统一工具返回值和证据格式。
4. **前端增强**：补充订单表格、趋势图和来源图片展示；已完成的编辑/确认表单、导入操作与错误状态见“已实现”。
5. **文档 RAG**：建立文档切分、索引、检索和来源展示，支持字段口径、系统说明等知识问答；订单事实仍以 SQL 为准。
6. **模型与效果评估**：完成 InternVL3-2B LoRA 微调、基线与微调指标（BLEU/Rouge）记录，并评估抽取准确率和问答正确率。
7. **生产化**：增加结构化日志、认证与权限、Redis 会话、SSE 流式响应、进程守护、限流和更严格的输入安全校验。
8. **远程模型服务**：按项目部署约定补齐 5003 端口的 LLaMA-Factory/OpenAI 兼容模型服务（模型名 `vl`），并与当前 Agent 做连通性验证。

## 当前运行配置

```dotenv
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen3.8-flash
AGENT_MODE=auto
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/chatbot
```

远程启动：

```bash
ssh server-4090
cd /workspace/team3/chatbot
conda activate team3
export DASHSCOPE_API_KEY="<服务器环境变量>"
# 推荐：使用一键脚本（自动 ssh 到 server-4090，tmux 常驻）
./scripts/manage.sh start
# 或手动：
python -m uvicorn agent.main:app --host 0.0.0.0 --port 8000
```

## 说明

- `AGENTS.md` 是项目操作约束文件，属于 Agent 工作指令，保留不作为进度说明。
- 本文件是项目唯一的进度/方案说明；旧的计划、联调、RAG 设计和状态文档已合并并删除。
