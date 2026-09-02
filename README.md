# DocuMind · 企业文档处理智能体系统

> **DocuMind**（Document + Mind）—— 基于多模态大模型的企业文档处理智能体系统。
> 输入扫描版发注书（PNG），抽取结构化订单信息（顾客公司、发注日、源公司、项目、数量、单价），
> 并通过对话式 Agent 提供数据查询、聚合统计、年度趋势、公司排名等智能问答。

## 功能特性

- **单据识别（Vision）**：上传 PNG 发注书，由多模态模型（默认 `qwen3.8-flash`，可切换 InternVL3-2B）抽取结构化字段，前端提供可编辑确认卡片，确认后幂等写入数据库。
- **订单问答（Agent + Function Tools）**：意图识别 + 白名单工具编排，模型选择工具、Python 执行 SQL，模型无法执行任意 SQL。
- **结构化分析**：按客户/年份查询明细、销售数量/金额聚合、年度趋势、公司排名、Python 最高/最低/平均统计。
- **多轮上下文**：同一 `conversation_id` 保留对话历史与上一轮查询结果，"这批/刚才/上述记录"类追问可复用。
- **高可用**：Qwen 工具编排不可用时自动回退到本地规则 Agent，服务不中断。
- **一键部署**：`scripts/deploy_remote.sh` 自动完成上传、依赖安装、PostgreSQL 初始化与 tmux 启动。

## 系统架构

```
用户 / 浏览器 ──> 前端单页应用 (frontend/index.html)
                      │  POST /api/chat (multipart: message + PNG)
                      ▼
              FastAPI 服务 (agent/api.py)
              ├─ 图片分支: model_extract / mock_extract ──> 前端确认 ──> POST /api/documents/import
              └─ 对话分支: model_agent (Qwen 工具调用) ──> execute_tool ──> PostgreSQL (orders)
                      ▲
              DashScope OpenAI 兼容 API (MODEL_BASE_URL)
```

详细架构图见 `architecture.drawio`（draw.io / diagrams.net 打开）。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Uvicorn · httpx · psycopg3 |
| 模型 | Qwen3.8-Flash（DashScope 兼容 API）· InternVL3-2B（LLaMA-Factory 微调，LoRA） |
| 数据 | PostgreSQL（orders 表）· openpyxl 导入标注数据 |
| 前端 | 原生 HTML/CSS/JavaScript（无构建步骤） |
| 部署 | SSH + rsync · tmux · conda(team3) / uv |

## 目录结构

```
.
├── agent/
│   ├── main.py          # FastAPI 入口（uvicorn agent.main:app）
│   └── api.py           # 核心逻辑：路由、意图识别、工具、模型调用、导入
├── frontend/
│   └── index.html       # 对话单页应用
├── scripts/
│   ├── deploy_remote.sh # 一键远程部署（server-4090）
│   ├── manage.sh        # 一键启动/停止/重启/状态（chatbot + InternVL vl）
│   ├── tunnel.sh        # SSH 端口转发 8000/5001/5003
│   └── init_db.py       # 建表 + 导入 output_label.xlsx
├── requirements.txt
├── .env.example         # 环境变量模板（不含真实密钥）
└── architecture.drawio  # 架构图
```

> 本地课程资产 `materials/`（训练数据 PNG + 标注 xlsx + 课件 PDF）**不随仓库分发**，请按需从本地放回后再执行部署脚本。

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt

# 配置环境变量（不提交真实密钥）
cp .env.example .env
export DASHSCOPE_API_KEY="你的Key"

# 启动（AGENT_MODE=mock 时可离线体验规则 Agent）
python -m uvicorn agent.main:app --host 0.0.0.0 --port 8000
```

### 远程部署（server-4090，GPU/模型服务）

```bash
# 一键部署：上传代码 -> uv 安装依赖 -> 初始化 PostgreSQL -> tmux 启动 8000 服务
./scripts/deploy_remote.sh

# 一键启动/守护（自动 ssh 到 server-4090；tmux 常驻，断连不退出）
./scripts/manage.sh start        # 启动 chatbot(8000) + InternVL vl(5003)
./scripts/manage.sh status       # 查看运行状态
./scripts/manage.sh restart      # 全部重启
./scripts/manage.sh stop         # 全部停止

# 本地访问远程服务（端口转发）
./scripts/tunnel.sh
# 然后访问 http://localhost:8000
```

## 环境配置（.env.example）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CHATBOT_HOST` | `0.0.0.0` | 服务监听地址 |
| `CHATBOT_PORT` | `8000` | 服务端口 |
| `MODEL_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容 API 地址 |
| `MODEL_NAME` | `qwen3.8-flash` | 模型名 |
| `MODEL_API_KEY` | 空 | 模型 Key（优先从 `DASHSCOPE_API_KEY` 读取） |
| `VISION_MODEL_BASE_URL` | `http://127.0.0.1:5003/v1` | 发注书识图模型 OpenAI 兼容 API（当前远程 `vl` 服务） |
| `VISION_MODEL_NAME` | `vl` | 识图模型名 |
| `VISION_MODEL_API_KEY` | 空 | 识图服务鉴权 Key（本地部署通常为空） |
| `MODEL_TIMEOUT_SECONDS` | `120` | 模型调用超时 |
| `MODEL_ENABLE_THINKING` | `true` | 是否启用模型思考 |
| `AGENT_MODE` | `auto` | `auto`=模型编排；`mock`=本地规则回退 |
| `DATABASE_URL` | `postgresql://postgres:postgres@127.0.0.1:5432/chatbot` | PostgreSQL 连接串 |
| `UPLOAD_DIR` | `/workspace/team3/chatbot/data/uploads` | 上传图片保存目录 |
| `MAX_UPLOAD_SIZE_MB` | `10` | 单图大小上限 |
| `ALLOW_ORIGINS` | `http://localhost:8000` | CORS 白名单（逗号分隔） |

## API 接口

Base URL：`http://localhost:8000`（Swagger 文档：`/docs`）

### `GET /health` — 健康检查

```bash
curl http://localhost:8000/health
```

响应：

```json
{
  "status": "ok",
  "agent": "ok",
  "database": "ok",
  "model": "configured",
  "version": "0.1.0"
}
```

- `status`：`ok` / `degraded`（数据库不可用时降级）
- `model`：`mock`（AGENT_MODE=mock）或 `configured`

### `GET /` — 对话页面

返回前端单页应用 `frontend/index.html`。

### `POST /api/chat` — 统一对话入口

`multipart/form-data` 表单，支持纯文本问答与 PNG 发注书上传。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | 与 files 二选一 | 用户问题 |
| `conversation_id` | string | 否 | 会话 ID，缺省自动生成；相同 ID 保留多轮上下文 |
| `client_request_id` | string | 是 | 客户端请求 ID（幂等/追踪） |
| `files` | file | 与 message 二选一 | 单个 PNG，≤10MB |

```bash
# 文本问答
curl -X POST http://localhost:8000/api/chat \
  -F "message=查询新日鉄住金ソリューションズ株式会社 2020 年的销售数据" \
  -F "client_request_id=req-001"

# 上传发注书识别
curl -X POST http://localhost:8000/api/chat \
  -F "message=请识别这张发注书" \
  -F "client_request_id=req-002" \
  -F "files=@/path/to/Sample1.png"
```

响应：

```json
{
  "conversation_id": "…",
  "message_id": "…",
  "trace_id": "…",
  "intent": "query_orders",
  "status": "completed",
  "answer": "自然语言回答…",
  "data": {
    "records": [],
    "summary": {"order_count": 0, "quantity": 0, "total_amount": 0},
    "trend": null,
    "ranking": null,
    "statistics": null,
    "database_status": null,
    "extraction": null,
    "import_result": null
  },
  "chart": null,
  "sources": [{"type": "database", "name": "orders"}],
  "warnings": [],
  "created_at": "2026-09-01T00:00:00"
}
```

**intent 取值**：`extract_document`（图片识别）、`query_orders`、`aggregate_sales`、`trend_analysis`、`company_ranking`、`python_statistics`、`database_status`、`import_document`、`general_chat`。

**错误响应**（HTTP 400）：

```json
{"code": "INVALID_IMAGE", "message": "仅支持不超过 10MB 的 PNG 图片"}
```

### `POST /api/documents/import` — 导入确认

图片识别后由前端渲染可编辑卡片，用户核对无误后提交本接口写入数据库（不经过模型）。

```bash
curl -X POST http://localhost:8000/api/documents/import \
  -H "Content-Type: application/json" \
  -d '{
    "client_request_id": "req-002",
    "image_name": "Sample1.png",
    "customer_company": "サンプル株式会社",
    "order_date": "2020-04-01",
    "source_company": "自社",
    "items": [
      {"project": "システム開発", "quantity": 10, "unit_price": 5000}
    ]
  }'
```

- 通过 `client_request_id` 幂等去重，重复提交返回首次结果。
- 数量/单价自动清洗（去除逗号、全角符号、`¥` 等），发注日校验为 `YYYY-MM-DD`。
- 错误：HTTP 400（数据非法） / HTTP 503（数据库不可用）。

### 模型服务（可选，端口 5001）

远程通过 LLaMA-Factory 部署的 OpenAI 兼容 API：

```bash
curl http://localhost:5001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "internvl3-2b", "messages": [{"role": "user", "content": "你好"}]}'
```

## 数据库（orders）

`scripts/init_db.py` 读取标注文件（`output_label.xlsx`）导入：

```sql
CREATE TABLE IF NOT EXISTS orders (
  id              SERIAL PRIMARY KEY,
  image_name      TEXT NOT NULL,
  customer_company TEXT,
  order_date      DATE,
  source_company  TEXT,
  project         TEXT,
  quantity        DOUBLE PRECISION,
  unit_price      DOUBLE PRECISION,
  total_amount    DOUBLE PRECISION,
  created_at      TIMESTAMPTZ DEFAULT now()
);
```

## Agent 工具（白名单）

模型只能选择以下工具，SQL 由 Python 侧参数化执行（杜绝任意 SQL 注入）：

| 工具 | 作用 |
|---|---|
| `query_orders` | 按客户/年份查询全部订单明细 |
| `aggregate_sales` | 统计订单条数、销售数量、销售金额 |
| `trend_analysis` | 按年度汇总数量与金额（趋势） |
| `company_ranking` | 按公司分组，返回交易额/销量前 N 名 |
| `python_statistics` | 对上一轮结果做最高/最低/平均等统计 |
| `database_status` | 查看数据库连接与数据概况 |

## 模型微调（InternVL3-2B）

- 框架：LLaMA-Factory（LoRA），`template: intern_vl`
- 数据：课程标注数据（PNG 发注书 + `output_label.xlsx` 字段标签）
- 评估：BLEU-4 / Rouge-1 / Rouge-2 / Rouge-L
- 产出：合并 LoRA 后的完整模型，通过 LLaMA-Factory `api` 部署到 5001 端口

## 可扩展方向

- **RAG**：建立文档切分、向量索引（pgvector）与来源检索，支持字段口径、系统说明、FAQ 等知识问答（订单事实仍以 SQL 为准）。
- **Workflow / 多智能体**：Dify 可视化工作流、LangGraph 多智能体编排；多跳任务（如 25 年与 24 年数据对比）。
- **生产化**：结构化日志、鉴权、Redis 会话、SSE 流式输出、限流。

## License

本项目为课程项目（信华信培训），仅供学习交流。
