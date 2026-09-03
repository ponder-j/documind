# DocuMind · Agent 系统架构

> 本文描述 `/agent` 模块化拆分后的**当前运行时架构**，配套 mermaid 图与技术栈说明。
> 代码入口：`agent/main.py` → `agent/api.py`（路由层）+ `agent/` 下功能模块。

---

## 1. 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 前端 | 原生 HTML/CSS/JavaScript | `frontend/index.html` 单页应用，无构建步骤 |
| 后端框架 | Python 3.12 · FastAPI · Uvicorn | 服务入口 `uvicorn agent.main:app`，端口 **8000** |
| HTTP 客户端 | httpx（Async） | 调用 DashScope 与本地视觉模型 OpenAI 兼容 API |
| 数据库 | PostgreSQL · psycopg3 | 库 `chatbot`，表 `orders` |
| 文本/Agent 模型 | Qwen3.8-Flash（DashScope 兼容 API） | `MODEL_BASE_URL` + `MODEL_API_KEY` / `DASHSCOPE_API_KEY` |
| 视觉模型 | InternVL3-2B（LLaMA-Factory LoRA 微调） | 本地部署为 OpenAI 兼容 API `vl`，端口 **5003**；SFT 输出固定 CSV 格式 |
| 状态管理 | 进程内 `SessionStore`（`state.py`） | 会话历史 / 工具上下文 / 导入幂等；单 worker 部署 |
| 部署 | SSH + rsync · tmux · conda(team3) / uv | `scripts/deploy_remote.sh` / `manage.sh` / `tunnel.sh` |

> 远程目录：`/workspace/team3/chatbot`；模型权重：`/workspace/team3/models/my_best_model`；
> tmux 会话：`chatbot-api`(:8000)、`vl-api`(:5003)；日志：`/workspace/team3/logs/`。

---

## 2. 系统总览

```mermaid
flowchart LR
    U["用户 / 浏览器"] --> FE["frontend/index.html<br/>HTML/CSS/JS 单页应用"]
    FE -->|"POST /api/chat<br/>multipart: message + PNG(可选)"| ROUTES

    subgraph API["FastAPI 服务 agent/ :8000"]
        ROUTES["api.py 路由层<br/>_handle_document_upload / _handle_text_message"]
        ROUTES -->|"文本问题"| AGENTS
        ROUTES -->|"上传 PNG"| VISION
        ROUTES -->|"确认导入 JSON"| IMPORTERS
    end

    AGENTS["agents.py<br/>model_agent / deterministic_agent / build_result"]
    AGENTS -->|"工具 Schema + 工具调用"| TOOLS["tools.py<br/>TOOL_SCHEMAS / execute_tool"]
    AGENTS -->|"规则回退: classify + extract_filters"| INTENT["intent.py<br/>意图识别 / 过滤抽取"]
    VISION["vision.py<br/>model_extract / parse_csv_extraction"]
    IMPORTERS["importers.py<br/>normalise_import_payload / persist_import"]

    TOOLS --> DB["db.py<br/>PostgreSQL 访问层"]
    INTENT --> DB
    IMPORTERS --> DB
    DB --> PG[("PostgreSQL<br/>orders")]

    AGENTS -->|"会话/上下文"| STATE["state.py<br/>SessionStore"]
    VISION --> STATE
    IMPORTERS --> STATE

    AGENTS -->|"OpenAI 兼容 /chat/completions"| QWEN["Qwen3.8-Flash<br/>DashScope 兼容 API"]
    VISION -->|"base64 PNG → CSV"| VL["InternVL3-2B 'vl'<br/>本地 LLaMA-Factory api :5003"]
    ROUTES --> CFG["config.py<br/>Settings(环境变量)"]
    CFG --> QWEN
    CFG --> VL
```

---

## 3. agent 包模块依赖

```mermaid
flowchart TD
    MAIN["main.py<br/>(uvicorn 入口)"] --> API
    API["api.py 路由层"] --> AGENTS["agents.py"]
    API --> VISION["vision.py"]
    API --> IMPORTERS["importers.py"]
    API --> DB["db.py"]
    API --> INTENT["intent.py"]
    API --> STATE["state.py"]
    API --> CFG["config.py"]

    AGENTS --> TOOLS["tools.py"]
    AGENTS --> DB
    AGENTS --> INTENT
    AGENTS --> STATE
    AGENTS --> CFG

    TOOLS --> DB
    TOOLS --> STATE

    INTENT --> DB
    IMPORTERS --> DB
    IMPORTERS --> STATE

    VISION --> CFG
    DB --> CFG

    style API fill:#e6f2ff
    style AGENTS fill:#fff2cc
    style DB fill:#e2efda
```

依赖方向自顶向下、无循环：路由层只做装配，业务逻辑按「意图 → 工具 → 数据」分层。

---

## 4. 文本问答（Function Tools 编排）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端
    participant API as api.py
    participant ST as state.py SessionStore
    participant AG as agents.py model_agent
    participant QW as "Qwen3.8-Flash (DashScope)"
    participant EX as tools.py execute_tool
    participant DB as db.py / PG
    participant FA as deterministic_agent (回退)
    participant IN as intent.py (classify/extract_filters)

    FE->>API: POST /api/chat (message, conversation_id)
    API->>AG: 非 import_document 且非 mock → model_agent
    AG->>ST: 追加 user 消息 / 读取 history[-40:]
    loop 最多 4 轮工具调用
        AG->>QW: messages + TOOL_SCHEMAS (tool_choice=auto)
        QW-->>AG: assistant 回复（文本 或 tool_calls）
        alt 无工具调用
            AG-->>API: build_result(answer, records, summary, trend...)
        else 有工具调用
            AG->>EX: execute_tool(name, args, cid)
            EX->>DB: query_orders / aggregate / trend / ranking / status
            EX-->>AG: 结果 + 写回 context(last_records...)
            AG->>QW: 追加 tool 消息（tool_result_for_model 压缩预览）
        end
    end
    Note over AG,EX: 白名单工具：query_orders, aggregate_sales, trend_analysis,<br/>company_ranking, python_statistics, database_status
    AG-->>API: 结构化响应（answer + data）
    API-->>FE: JSON（intent/summary/trend/ranking/statistics/...）

    rect rgb(255,243,224)
    Note over API,FA: 模型不可用/异常时自动回退
    API->>FA: deterministic_agent(message, cid)
    FA->>IN: classify + extract_filters
    FA->>DB: 同白名单工具（纯本地规则，无模型）
    FA-->>API: 相同响应结构（build_result）
    end
```

---

## 5. 单据识别 → 确认导入

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端
    participant API as api.py
    participant ST as state.py SessionStore
    participant VI as vision.py
    participant VL as "InternVL3-2B 'vl' (:5003)"
    participant IM as importers.py
    participant PG as PostgreSQL orders

    FE->>API: POST /api/chat (files=Sample.png)
    API->>API: 校验 PNG 且 ≤ 10MB；落盘 UPLOAD_DIR
    API->>VI: model_extract(base64)
    VI->>VL: OpenAI 兼容 /chat/completions（图片 + CSV 提示词）
    VL-->>VI: CSV 文本 (desc,date,from,item,amount,price)
    VI-->>API: parse_csv_extraction → 结构化 dict
    API->>ST: 保存 pending_extraction（清空旧 pending_*）
    API-->>FE: data.extraction + 格式化 answer
    FE->>FE: 渲染可编辑「识别结果」卡片
    FE->>API: 确认导入 → POST /api/documents/import (JSON)
    API->>IM: persist_import(payload, client_request_id)
    IM->>IM: normalise_import_payload（数字/日期校验、清洗）
    IM->>PG: INSERT ... RETURNING id（幂等：同 key 不重复插入）
    PG-->>IM: record_ids
    IM->>ST: 记录 seen_imports[key]（幂等缓存）
    IM-->>API: {status: imported, record_ids, imported_count}
    API-->>FE: 导入结果

    Note over API,FE: mock 模式（AGENT_MODE=mock）不调用视觉模型，<br/>直接返回 mock_extract 占位结果，便于本地联调
```

---

## 6. 远程部署拓扑（server-4090）

```mermaid
flowchart LR
    subgraph Local["本地开发机 (macOS)"]
        CODE["项目代码 /agent, /frontend, /scripts"]
        TUN["ssh -L 8000/5003 端口转发<br/>scripts/tunnel.sh"]
        BR["浏览器访问 http://localhost:8000"]
    end

    subgraph Remote["远程服务器 server-4090<br/>Ubuntu 22.04 · 4×RTX4090 · conda team3 / uv"]
        subgraph TMUX["tmux 会话"]
            CB["chatbot-api<br/>uvicorn agent.main:app :8000<br/>.venv (Python 3.12)"]
            VL["vl-api<br/>llamafactory-cli api<br/>InternVL3-2B (LoRA 合并) :5003"]
        end
        CB --> VL
        PG[("PostgreSQL :5432<br/>chatbot.orders")]
        CB --> PG
    end

    CODE -->|"rsync 一键部署<br/>scripts/deploy_remote.sh"| TMUX
    BR --> TUN --> CB
```

---

## 7. 关键接口与数据结构

### 7.1 HTTP 接口（FastAPI :8000）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 返回前端单页 `frontend/index.html` |
| `/health` | GET | 健康检查（agent / database / model 状态） |
| `/api/chat` | POST | multipart：`message` + 可选 `files`(PNG) + `conversation_id` + `client_request_id` |
| `/api/documents/import` | POST | JSON：`image_name, customer_company, order_date, source_company, items[], client_request_id` |

### 7.2 Agent 工具（白名单，模型不可直接执行 SQL）

| 工具 | 用途 | 实现 |
|---|---|---|
| `query_orders` | 按客户/年份查询订单明细 + 汇总 | `db.query_orders_dataset` |
| `aggregate_sales` | 条数 / 销售数量 / 销售金额聚合 | `db.aggregate_sales_tool` |
| `trend_analysis` | 年度销量/金额趋势 | `db.trend_tool` |
| `company_ranking` | 按客户公司排名（交易额/销量，前 N） | `db.company_ranking_tool` |
| `python_statistics` | 最高/最低/平均/前后 5 名（Python 统计） | `tools.python_statistics_tool` |
| `database_status` | 连接 / 表 / 记录数 / 客户数 / 日期范围 | `db.database_status_tool` |

### 7.3 `orders` 表结构

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | SERIAL PK | 主键 |
| `image_name` | TEXT NOT NULL | 来源单据文件名 |
| `customer_company` | TEXT | 顾客公司（买方） |
| `order_date` | DATE | 发注日（YYYY-MM-DD） |
| `source_company` | TEXT | 源公司（卖方） |
| `project` | TEXT | 项目/商品名 |
| `quantity` | DOUBLE PRECISION | 数量 |
| `unit_price` | DOUBLE PRECISION | 单价 |
| `total_amount` | DOUBLE PRECISION | 金额（导入时按 数量×单价 计算） |
| `created_at` | TIMESTAMPTZ | 入库时间 |

### 7.4 关键环境变量（见 `.env.example`）

`MODEL_BASE_URL` / `MODEL_NAME`(qwen3.8-flash) / `MODEL_API_KEY`·`DASHSCOPE_API_KEY` /
`VISION_MODEL_BASE_URL`(http://127.0.0.1:5003/v1) / `VISION_MODEL_NAME`(vl) /
`MODEL_TIMEOUT_SECONDS` / `MODEL_ENABLE_THINKING` / `AGENT_MODE`(auto|mock) /
`DATABASE_URL` / `UPLOAD_DIR` / `MAX_UPLOAD_SIZE_MB` / `ALLOW_ORIGINS` / `CHATBOT_HOST` / `CHATBOT_PORT`
