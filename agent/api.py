import base64, json, math, os, re, statistics, uuid
from datetime import datetime
from pathlib import Path
import httpx
import psycopg
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Qwen3.8-Flash via DashScope compatible-mode API.  Keep the key in the
    # DASHSCOPE_API_KEY environment variable (never commit it to source).
    model_base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    model_name: str = 'qwen3.8-flash'
    model_api_key: str = ''
    model_timeout_seconds: int = 120
    model_enable_thinking: bool = True
    agent_mode: str = 'auto'
    database_url: str = 'postgresql://postgres:postgres@127.0.0.1:5432/chatbot'
    upload_dir: str = '/workspace/forth/data/uploads'; max_upload_size_mb: int = 10
    allow_origins: str = 'http://localhost:8000'
settings=Settings()
if not settings.model_api_key:
    import os
    settings.model_api_key = os.getenv('DASHSCOPE_API_KEY', '')
app=FastAPI(title='企业文档处理对话智能体', version='0.1.0')
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.allow_origins.split(',')], allow_methods=['*'], allow_headers=['*'])
sessions={}; session_context={}; seen_imports={}

# The model chooses among these read-only tools.  Database access remains in
# Python so the model can never execute arbitrary SQL supplied by a user.
TOOL_SCHEMAS = [
    {'type': 'function', 'function': {
        'name': 'query_orders',
        'description': '按客户和年份查询全部订单明细；返回完整匹配结果及汇总数量。',
        'parameters': {'type': 'object', 'properties': {
            'customer_company': {'type': ['string', 'null'], 'description': '客户公司全名或关键词'},
            'years': {'type': 'array', 'items': {'type': 'integer'}, 'description': '年份，例如 [2020]'},
        }, 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'aggregate_sales',
        'description': '统计匹配订单的条数、销售数量和销售金额。',
        'parameters': {'type': 'object', 'properties': {
            'customer_company': {'type': ['string', 'null'], 'description': '客户公司全名或关键词'},
            'years': {'type': 'array', 'items': {'type': 'integer'}, 'description': '年份，例如 [2020]'},
        }, 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'trend_analysis',
        'description': '按年度汇总销售数量和销售金额，用于趋势分析。',
        'parameters': {'type': 'object', 'properties': {
            'customer_company': {'type': ['string', 'null'], 'description': '客户公司全名或关键词'},
            }, 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'company_ranking',
        'description': '按客户公司分组汇总订单，返回总交易额或销售数量最高的前N家公司。适用于“总交易额前10名的公司”“销售量排名前5家公司”等公司级排名，不是单笔订单排名。',
        'parameters': {'type': 'object', 'properties': {
            'metric': {'type': 'string', 'enum': ['total_amount', 'quantity'], 'description': '排名指标：total_amount=总交易额/销售金额，quantity=销售数量'},
            'top_n': {'type': 'integer', 'description': '返回前N名，通常为 10'},
            'customer_company': {'type': ['string', 'null'], 'description': '可选的客户公司关键词；通常留空表示全部公司'},
            'years': {'type': 'array', 'items': {'type': 'integer'}, 'description': '可选年份筛选，例如 [2020, 2021]'},
        }, 'required': ['metric', 'top_n'], 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'python_statistics',
        'description': '使用 Python 对已查询的订单明细做最高/最低/平均等统计；分析“这批/刚才/上述记录”时 scope 使用 last_query。公司级排名必须使用 company_ranking。',
        'parameters': {'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': ['max_min', 'summary', 'top_bottom'], 'description': '统计类型'},
            'scope': {'type': 'string', 'enum': ['last_query', 'database'], 'default': 'last_query'},
            'customer_company': {'type': ['string', 'null']},
            'years': {'type': 'array', 'items': {'type': 'integer'}},
        }, 'required': ['operation'], 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'database_status',
        'description': '查看数据库连接、orders 表是否存在、记录数、客户数和日期范围。',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }},
]

def db(): return psycopg.connect(settings.database_url)
def init_db():
    try:
        with db() as c: c.execute('''CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY,image_name TEXT NOT NULL,customer_company TEXT,order_date DATE,source_company TEXT,project TEXT,quantity DOUBLE PRECISION,unit_price DOUBLE PRECISION,total_amount DOUBLE PRECISION,created_at TIMESTAMPTZ DEFAULT now())''')
    except Exception: pass
init_db()

def classify(msg):
    if any(k in msg for k in ('导入数据库', '确认导入', '写入数据库', '保存到数据库', '入库')):
        return 'import_document'
    if any(k in msg for k in ('当前数据库', '数据库情况', '数据库状态', '表结构', '字段范围', '数据量', '数据总量', '有多少条数据', '有多少条记录')):
        return 'database_status'
    if any(k in msg for k in ('前10','前 10','前5','前 5','前几名','排名','排行','榜单')) and any(k in msg for k in ('公司','客户','交易额','销售额','销售量','数量')):
        return 'company_ranking'
    if any(k in msg for k in ('趋势','变化','走势')): return 'trend_analysis'
    if any(k in msg for k in ('最高','最低','最大','最小','平均','均值','排序','排名')): return 'python_statistics'
    if any(k in msg for k in ('多少件','数量','总金额','总额','交易额','销售额')): return 'aggregate_sales'
    if any(k in msg for k in ('查询','销售数据','订单')): return 'query_orders'
    return 'general_chat'

def extract_filters(message):
    years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?!\d)', message)]
    # Remove request verbs before extracting a company; otherwise "查询示例公司"
    # would be searched literally and never match the stored "示例公司" value.
    cleaned = re.sub(
        r'^\s*(?:(?:请问|请|麻烦|帮我|帮忙|能否|可以)\s*)*'
        r'(?:查询|查一下|查找|查看|获取|列出|统计|分析|告诉我)\s*',
        '', message.strip(), flags=re.I,
    )
    # Prefer an exact/longest match against the companies actually stored in the
    # database. This handles Japanese prefix forms such as "株式会社NTTデータ"
    # without accidentally reducing them to the suffix "株式会社".
    company = None
    try:
        with db() as c:
            names = [r[0] for r in c.execute('SELECT DISTINCT customer_company FROM orders WHERE customer_company IS NOT NULL').fetchall()]
        matches = [name for name in names if name and name in cleaned]
        if matches:
            company = max(matches, key=len)
    except Exception:
        # Filtering should still work when the database is temporarily down;
        # the SQL tool will report the actual connection error to the caller.
        pass
    if company is None:
        # Fallback for a company not yet present in the DB (useful for diagnostics).
        m = re.search(r'([^\s，。,.、！？?]+?(?:股份有限公司|有限责任公司|有限公司|株式会社|公司))', cleaned)
        if m: company = m.group(1).strip()
    return {'years': years, 'customer_company': company}

ORDER_FIELDS = ('image_name','customer_company','order_date','source_company','project','quantity','unit_price','total_amount')

def _where_clause(filters):
    clauses, args = [], []
    if filters.get('customer_company'):
        clauses.append('customer_company ILIKE %s'); args.append('%'+filters['customer_company']+'%')
    if filters.get('years'):
        clauses.append('EXTRACT(YEAR FROM order_date) = ANY(%s)'); args.append(filters['years'])
    return ((' WHERE ' + ' AND '.join(clauses)) if clauses else ''), args

def _record_dicts(rows):
    return [dict(zip(ORDER_FIELDS, r)) for r in rows]

def query_orders_dataset(filters):
    where, args = _where_clause(filters)
    with db() as c:
        total = c.execute('SELECT COUNT(*) FROM orders'+where, args).fetchone()[0]
        totals = c.execute('SELECT COALESCE(SUM(quantity),0), COALESCE(SUM(total_amount),0) FROM orders'+where, args).fetchone()
        rows = c.execute('SELECT image_name,customer_company,order_date,source_company,project,quantity,unit_price,total_amount FROM orders'+where+' ORDER BY order_date DESC NULLS LAST', args).fetchall()
    return {'records': _record_dicts(rows), 'total_matches': total, 'total_quantity': float(totals[0]), 'total_amount': float(totals[1]), 'returned_count': len(rows)}

def query_orders_tool(filters):
    return query_orders_dataset(filters)['records']

def aggregate_sales_tool(filters):
    where, args = _where_clause(filters)
    with db() as c:
        r = c.execute('SELECT COUNT(*), COALESCE(SUM(quantity),0), COALESCE(SUM(total_amount),0) FROM orders'+where, args).fetchone()
    return {'order_count': r[0], 'quantity': float(r[1]), 'total_amount': float(r[2])}

def trend_tool(filters):
    where, args = _where_clause(filters)
    with db() as c:
        rows = c.execute('SELECT EXTRACT(YEAR FROM order_date)::int, COALESCE(SUM(quantity),0), COALESCE(SUM(total_amount),0) FROM orders'+where+' GROUP BY 1 ORDER BY 1', args).fetchall()
    return [{'year': r[0], 'quantity': float(r[1]), 'total_amount': float(r[2])} for r in rows]

def company_ranking_tool(args):
    """Aggregate orders by customer company and return a deterministic ranking."""
    args = args if isinstance(args, dict) else {}
    metric = args.get('metric') if args.get('metric') in ('total_amount', 'quantity') else 'total_amount'
    try:
        top_n = int(args.get('top_n', 10))
    except (TypeError, ValueError):
        top_n = 10
    top_n = max(1, min(top_n, 100))
    # Reuse the same validation as the other database tools so model-generated
    # strings such as "2020" cannot leak into SQL parameter binding.
    filters = _normalise_filters(args)
    where, query_args = _where_clause(filters)
    company_clause = 'customer_company IS NOT NULL AND BTRIM(customer_company) <> %s'
    if where:
        where += ' AND ' + company_clause
    else:
        where = ' WHERE ' + company_clause
    query_args.append('')
    order_expr = 'COALESCE(SUM(total_amount), 0)' if metric == 'total_amount' else 'COALESCE(SUM(quantity), 0)'
    sql = (
        'SELECT customer_company, COUNT(*) AS order_count, '
        'COALESCE(SUM(quantity), 0) AS quantity, '
        'COALESCE(SUM(total_amount), 0) AS total_amount '
        'FROM orders' + where +
        ' GROUP BY customer_company '
        f'ORDER BY {order_expr} DESC, customer_company ASC LIMIT %s'
    )
    query_args.append(top_n)
    with db() as c:
        rows = c.execute(sql, query_args).fetchall()
    return [
        {
            'customer_company': row[0],
            'order_count': int(row[1]),
            'quantity': float(row[2] or 0),
            'total_amount': float(row[3] or 0),
        }
        for row in rows
    ]

def _jsonable_record(record):
    return {k: (str(v) if hasattr(v, 'isoformat') else v) for k, v in record.items()}

def python_statistics_tool(args, cid):
    """Run deterministic Python statistics over the last query or DB rows."""
    operation = args.get('operation', 'summary')
    scope = args.get('scope', 'last_query')
    context = session_context.setdefault(cid, {})
    if scope == 'last_query' and context.get('last_records') is not None:
        records = context['last_records']
    else:
        filters = _normalise_filters(args)
        # Database-scope statistics always cover every matching row.
        records = query_orders_tool(filters)
    amounts = []
    for record in records:
        amount = record.get('total_amount')
        if amount is None and record.get('quantity') is not None and record.get('unit_price') is not None:
            amount = record['quantity'] * record['unit_price']
        if amount is not None:
            amounts.append((float(amount), record))
    result = {'scope': scope, 'operation': operation, 'record_count': len(records)}
    if not records:
        return result
    result['quantity_sum'] = sum(float(r.get('quantity') or 0) for r in records)
    result['sales_amount_sum'] = sum(amount for amount, _ in amounts)
    if amounts:
        result['average_sales_amount'] = statistics.fmean(amount for amount, _ in amounts)
        if operation in ('max_min', 'top_bottom'):
            result['maximum'] = _jsonable_record(max(amounts, key=lambda x: x[0])[1])
            result['maximum']['sales_amount'] = max(amounts, key=lambda x: x[0])[0]
            result['minimum'] = _jsonable_record(min(amounts, key=lambda x: x[0])[1])
            result['minimum']['sales_amount'] = min(amounts, key=lambda x: x[0])[0]
        if operation == 'top_bottom':
            result['top_5'] = [{**_jsonable_record(row), 'sales_amount': amount} for amount, row in sorted(amounts, reverse=True, key=lambda x: x[0])[:5]]
            result['bottom_5'] = [{**_jsonable_record(row), 'sales_amount': amount} for amount, row in sorted(amounts, key=lambda x: x[0])[:5]]
    return result

def database_status_tool():
    """Return safe, read-only metadata so the agent can explain DB availability."""
    with db() as c:
        table = c.execute("SELECT to_regclass('public.orders')").fetchone()[0]
        if table is None:
            return {'connected': True, 'table': 'orders', 'exists': False, 'record_count': 0}
        record_count = c.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        companies = c.execute('SELECT COUNT(DISTINCT customer_company) FROM orders').fetchone()[0]
        date_min, date_max = c.execute('SELECT MIN(order_date), MAX(order_date) FROM orders').fetchone()
        return {
            'connected': True,
            'table': 'orders',
            'exists': True,
            'record_count': record_count,
            'company_count': companies,
            'date_min': date_min,
            'date_max': date_max,
        }

def _number(value, field, item_index):
    """Convert OCR/model numeric text to a database-safe float."""
    if value is None or (isinstance(value, str) and not value.strip()): return None
    if isinstance(value, bool): raise ValueError(f'第 {item_index} 条明细的{field}不是有效数字')
    if isinstance(value, str): value=value.strip().translate(str.maketrans('，,￥¥　', ',,,, ')).replace(',', '')
    try: number=float(value)
    except (TypeError, ValueError): raise ValueError(f'第 {item_index} 条明细的{field}不是有效数字') from None
    if not math.isfinite(number) or number < 0: raise ValueError(f'第 {item_index} 条明细的{field}必须是非负数字')
    return number

def _order_date(value):
    if value is None or not str(value).strip(): return None
    value=str(value).strip().replace('/', '-').replace('.', '-')
    match=re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', value)
    if not match: raise ValueError('发注日格式应为 YYYY-MM-DD')
    try: return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError: raise ValueError('发注日不是有效日期') from None

def normalise_import_payload(payload):
    if not isinstance(payload, dict): raise ValueError('导入数据格式错误')
    items=payload.get('items')
    if not isinstance(items, list) or not items: raise ValueError('没有可导入的明细')
    out={'image_name':str(payload.get('image_name') or ''),'customer_company':payload.get('customer_company'),'order_date':_order_date(payload.get('order_date')),'source_company':payload.get('source_company'),'items':[]}
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict): raise ValueError(f'第 {index} 条明细格式错误')
        out['items'].append({'project':item.get('project'),'quantity':_number(item.get('quantity'), '数量', index),'unit_price':_number(item.get('unit_price'), '单价', index)})
    return out

def persist_import(payload, key):
    """Insert one extracted document idempotently and return its IDs."""
    if key in seen_imports:
        return seen_imports[key]
    payload = normalise_import_payload(payload)
    items = payload['items']
    ids = []
    with db() as c:
        for item in items:
            quantity = item.get('quantity')
            unit_price = item.get('unit_price')
            total = (quantity or 0) * (unit_price or 0) if quantity is not None and unit_price is not None else None
            row = c.execute(
                'INSERT INTO orders(image_name,customer_company,order_date,source_company,project,quantity,unit_price,total_amount) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (payload.get('image_name', ''), payload.get('customer_company'), payload.get('order_date'), payload.get('source_company'), item.get('project'), quantity, unit_price, total),
            ).fetchone()
            ids.append(row[0])
    out = {'status': 'imported', 'document_id': str(uuid.uuid4()), 'record_ids': ids, 'imported_count': len(ids), 'trace_id': str(uuid.uuid4())}
    seen_imports[key] = out
    return out

def _normalise_filters(args):
    years = args.get('years') or []
    if not isinstance(years, list): years = [years]
    years = [int(y) for y in years if str(y).isdigit()]
    company = args.get('customer_company')
    if isinstance(company, str):
        company = company.strip()
        # Qwen occasionally emits a JSON string containing the word `null`
        # (with surrounding whitespace/newlines) instead of a JSON null. Treat
        # common whole-scope placeholders as no company filter.
        if company.casefold() in {'null', 'none', 'n/a', 'na', '无', '全部', '全部客户', '所有客户', '各客户', '所有公司', '各公司'}:
            company = None
    elif not company:
        company = None
    else:
        company = str(company).strip()
    return {'customer_company': company, 'years': years}

def execute_tool(name, args, cid):
    """Dispatch only allow-listed tools and retain data for follow-up turns."""
    args = args if isinstance(args, dict) else {}
    context = session_context.setdefault(cid, {})
    if name == 'query_orders':
        # Do not paginate: the API returns every matching order. Only the
        # compact preview sent back to Qwen is sampled to control prompt size.
        dataset = query_orders_dataset(_normalise_filters(args))
        context['last_records'] = dataset['records']
        context['last_query_meta'] = dataset
        context['last_query'] = _normalise_filters(args)
        return dataset
    if name == 'aggregate_sales':
        context['last_query'] = _normalise_filters(args)
        return aggregate_sales_tool(context['last_query'])
    if name == 'trend_analysis':
        context['last_query'] = _normalise_filters(args)
        return trend_tool(context['last_query'])
    if name == 'company_ranking':
        result = company_ranking_tool(args)
        context['last_ranking'] = result
        return result
    if name == 'python_statistics':
        return python_statistics_tool(args, cid)
    if name == 'database_status':
        return database_status_tool()
    raise ValueError(f'不允许调用工具: {name}')

def tool_result_for_model(name, result):
    """Keep the conversation compact while Python retains the complete rows."""
    if name == 'query_orders' and isinstance(result, dict):
        return {
            'total_matches': result.get('total_matches', 0),
            'total_quantity': result.get('total_quantity', 0),
            'total_amount': result.get('total_amount', 0),
            'returned_count': result.get('returned_count', 0),
            'records_preview': [_jsonable_record(r) for r in result.get('records', [])[:20]],
            'preview_count': min(result.get('returned_count', 0), 20),
        }
    if name == 'python_statistics' and isinstance(result, dict):
        return result
    if name == 'company_ranking' and isinstance(result, list):
        return {'ranking': result, 'returned_count': len(result)}
    return result

def _content_text(value):
    if isinstance(value, list):
        return ''.join((x.get('text', '') if isinstance(x, dict) else str(x)) for x in value)
    return str(value or '')

async def model_agent(message, cid):
    """Let Qwen select tools, then let Python execute and explain their results."""
    if not settings.model_api_key:
        raise RuntimeError('MODEL_API_KEY/DASHSCOPE_API_KEY 未配置')
    history = sessions.setdefault(cid, [])
    history.append({'role': 'user', 'content': message})
    system = (
        '你是企业订单数据智能体。请用中文回答，事实问题必须调用工具，不能凭空编造。'
        '涉及订单、销售、客户、年份、数量、金额或数据库状态时，先选择合适的工具。'
        '“这批/上述/刚才这些记录”等追问必须调用 python_statistics，scope 使用 last_query；'
        'python_statistics 会在服务器端用 Python 对上一轮完整结果统计。'
        'query_orders 返回全部明细及 total_matches；若用户要最高、最低、平均、排序，必须调用 python_statistics。'
        '用户要求按公司/客户分组、排名、前N名、榜单时，必须调用 company_ranking；'
        '总交易额/销售额使用 metric=total_amount，销售数量/销量使用 metric=quantity，top_n 使用用户指定的数字。'
        '不要把公司级排名误当成单笔订单最高，也不要在未调用工具时猜测数据库结果。'
        '导入数据库只能由页面的“确认导入”按钮完成；你不能调用或模拟导入操作。'
        '工具返回后，只根据工具结果作简洁、可核验的回答，并说明统计范围。'
    )
    last_tool_name = None
    last_tool_result = None
    for _ in range(4):
        payload = {
            'model': settings.model_name,
            'messages': [{'role': 'system', 'content': system}] + history[-40:],
            'tools': TOOL_SCHEMAS,
            'tool_choice': 'auto',
            'temperature': 0.1,
            'max_tokens': 1200,
            'enable_thinking': settings.model_enable_thinking,
        }
        async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
            response = await client.post(settings.model_base_url + '/chat/completions', headers={'Authorization': 'Bearer ' + settings.model_api_key}, json=payload)
            response.raise_for_status()
            body = response.json()
        choice = (body.get('choices') or [{}])[0]
        assistant = choice.get('message') or {}
        calls = assistant.get('tool_calls') or []
        if not calls:
            answer = _content_text(assistant.get('content')).strip()
            if not answer:
                raise RuntimeError('Qwen 未返回文本或工具调用')
            history.append({'role': 'assistant', 'content': answer})
            context = session_context.get(cid, {})
            return {
                'answer': answer,
                'intent': {'query_orders': 'query_orders', 'aggregate_sales': 'aggregate_sales', 'trend_analysis': 'trend_analysis', 'company_ranking': 'company_ranking', 'python_statistics': 'python_statistics', 'database_status': 'database_status'}.get(last_tool_name, classify(message)),
                'records': context.get('last_records', []),
                'summary': last_tool_result if last_tool_name == 'aggregate_sales' else (
                    {'order_count': sum(int(r.get('order_count') or 0) for r in last_tool_result), 'quantity': sum(float(r.get('quantity') or 0) for r in last_tool_result), 'total_amount': sum(float(r.get('total_amount') or 0) for r in last_tool_result)}
                    if last_tool_name == 'company_ranking' and isinstance(last_tool_result, list) else
                    {'order_count': context.get('last_query_meta', {}).get('total_matches', len(context.get('last_records', []))), 'quantity': context.get('last_query_meta', {}).get('total_quantity', sum(float(r.get('quantity') or 0) for r in context.get('last_records', []))), 'total_amount': context.get('last_query_meta', {}).get('total_amount', sum(float(r.get('total_amount') or 0) for r in context.get('last_records', []))) }
                ),
                'trend': last_tool_result if last_tool_name == 'trend_analysis' else None,
                'statistics': last_tool_result if last_tool_name == 'python_statistics' else None,
                'ranking': last_tool_result if last_tool_name == 'company_ranking' else None,
                'database_status': last_tool_result if last_tool_name == 'database_status' else None,
                'import_result': None,
            }
        history.append({'role': 'assistant', 'content': _content_text(assistant.get('content')) or None, 'tool_calls': calls})
        for call in calls:
            function = call.get('function') or {}
            name = function.get('name', '')
            raw_arguments = function.get('arguments') or '{}'
            if isinstance(raw_arguments, dict):
                # Some OpenAI-compatible gateways already decode function
                # arguments before returning them; do not feed a dict to
                # json.loads (which raises TypeError).
                arguments = raw_arguments
            elif isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
            else:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            result = execute_tool(name, arguments, cid)
            last_tool_name, last_tool_result = name, result
            history.append({'role': 'tool', 'tool_call_id': call.get('id', str(uuid.uuid4())), 'name': name, 'content': json.dumps(tool_result_for_model(name, result), ensure_ascii=False, default=str)})
    raise RuntimeError('工具调用超过最大轮次')

def deterministic_agent(message, cid):
    """Safe fallback when the model endpoint is unavailable or rejects tools."""
    intent = classify(message)
    filters = extract_filters(message)
    context = session_context.setdefault(cid, {})
    if intent == 'query_orders':
        dataset = query_orders_dataset(filters)
        context['last_records'], context['last_query_meta'], context['last_query'] = dataset['records'], dataset, filters
        answer = f"已查询到 {dataset['total_matches']} 条订单记录。"
        return {'answer': answer, 'intent': intent, 'records': dataset['records'], 'summary': {'order_count': dataset['total_matches'], 'quantity': sum(float(r.get('quantity') or 0) for r in dataset['records']), 'total_amount': sum(float(r.get('total_amount') or 0) for r in dataset['records'])}}
    if intent == 'aggregate_sales':
        summary = aggregate_sales_tool(filters)
        return {'answer': f"根据数据库记录，共 {summary['order_count']} 条订单，销售数量 {summary['quantity']:g} 件，销售金额 {summary['total_amount']:,.2f}。", 'intent': intent, 'records': [], 'summary': summary}
    if intent == 'trend_analysis':
        trend = trend_tool(filters)
        answer = '年度销售趋势：' + '；'.join(f"{x['year']}年 {x['quantity']:g} 件" for x in trend) if trend else '暂无可用于趋势分析的订单数据。'
        return {'answer': answer, 'intent': intent, 'records': [], 'summary': {'order_count': len(trend), 'quantity': sum(x['quantity'] for x in trend), 'total_amount': sum(x['total_amount'] for x in trend)}, 'trend': trend}
    if intent == 'company_ranking':
        metric = 'quantity' if any(k in message for k in ('销售量', '销量', '销售数量', '数量')) and not any(k in message for k in ('交易额', '销售额', '金额')) else 'total_amount'
        top_match = re.search(r'前\s*(\d+)\s*(?:名|家|个)', message)
        top_n = int(top_match.group(1)) if top_match else 10
        ranking = company_ranking_tool({'metric': metric, 'top_n': top_n, **filters})
        label = '销售数量' if metric == 'quantity' else '总交易额'
        if not ranking:
            answer = '数据库中没有可用于公司排名的订单记录。'
        else:
            metric_key = 'quantity' if metric == 'quantity' else 'total_amount'
            answer = '按{}排名前{}名：\n'.format(label, len(ranking)) + '\n'.join(f"{i}. {row['customer_company']}：{row[metric_key]:,.2f}" for i, row in enumerate(ranking, 1))
        return {'answer': answer, 'intent': intent, 'records': [], 'summary': {'order_count': sum(row['order_count'] for row in ranking), 'quantity': sum(row['quantity'] for row in ranking), 'total_amount': sum(row['total_amount'] for row in ranking)}, 'ranking': ranking}
    if intent == 'python_statistics':
        scope = 'last_query' if context.get('last_records') is not None else 'database'
        stats = python_statistics_tool({'operation': 'max_min', 'scope': scope, **filters}, cid)
        if not stats.get('record_count'):
            answer = '没有可供 Python 统计的订单记录。'
        else:
            answer = (f"已用 Python 统计 {stats['record_count']} 条记录：销售额最高为 {stats['maximum']['sales_amount']:,.2f}，"
                      f"最低为 {stats['minimum']['sales_amount']:,.2f}。")
        return {'answer': answer, 'intent': intent, 'records': context.get('last_records', []), 'summary': {'order_count': stats.get('record_count', 0), 'quantity': stats.get('quantity_sum', 0), 'total_amount': stats.get('sales_amount_sum', 0)}, 'statistics': stats}
    if intent == 'database_status':
        status = database_status_tool()
        answer = (f"数据库连接正常，orders 表已有 {status['record_count']:,} 条订单记录，包含 {status['company_count']} 家客户，日期范围为 {status['date_min']} 至 {status['date_max']}。" if status.get('exists') else '数据库连接正常，但尚未创建 orders 表。')
        return {'answer': answer, 'intent': intent, 'records': [], 'summary': {'order_count': 0, 'quantity': 0, 'total_amount': 0}, 'database_status': status}
    if intent == 'import_document':
        return {'answer': '请在识别结果卡片中核对或修改字段，然后点击“确认导入”。', 'intent': intent, 'records': [], 'summary': {'order_count': 0, 'quantity': 0, 'total_amount': 0}}
    answer = '我可以识别发注书、查询订单、统计销售数据并分析趋势。'
    return {'answer': answer, 'intent': intent, 'records': [], 'summary': {'order_count': 0, 'quantity': 0, 'total_amount': 0}}
def mock_extract(name): return {'customer_company':'待确认顾客公司','order_date':None,'source_company':'待确认源公司','items':[{'project':'待确认项目','quantity':0,'unit_price':0}], 'image_name':name}
def _display(value, fallback='未识别'):
    if value is None or value == '': return fallback
    return str(value)

def format_extraction_answer(extraction):
    lines = ['已完成发注书识别，请核对以下内容：',
             f"• 顾客公司：{_display(extraction.get('customer_company'))}",
             f"• 发注日：{_display(extraction.get('order_date'))}",
             f"• 源公司：{_display(extraction.get('source_company'))}"]
    items = extraction.get('items') or []
    if items:
        lines.append('• 明细：')
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {_display(item.get('project'))}，数量 {_display(item.get('quantity'))}，单价 {_display(item.get('unit_price'))}")
    else: lines.append('• 明细：未识别到项目')
    lines.append('确认无误后即可导入数据库。')
    return '\n'.join(lines)
def parse_json_object(text):
    """Extract the first JSON object from Qwen output (reasoning/Markdown safe)."""
    if isinstance(text, list):
        text=''.join((x.get('text','') if isinstance(x, dict) else str(x)) for x in text)
    text=str(text).strip()
    text=re.sub(r'<think>.*?</think>', '', text, flags=re.S|re.I)
    text=re.sub(r'```(?:json)?', '', text, flags=re.I).replace('```','').strip()
    start=text.find('{'); end=text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('模型未返回可解析的 JSON')
    return json.loads(text[start:end+1])
async def model_extract(data,name):
    prompt='只返回JSON：{"customer_company":null,"order_date":null,"source_company":null,"items":[{"project":null,"quantity":null,"unit_price":null}]}'
    content=[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(data).decode()}}]
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as c:
        r=await c.post(settings.model_base_url+'/chat/completions',headers={'Authorization':'Bearer '+settings.model_api_key},json={'model':settings.model_name,'messages':[{'role':'user','content':content}],'temperature':0,'max_tokens':1024,'enable_thinking':False}); r.raise_for_status(); text=r.json()['choices'][0]['message'].get('content',''); return parse_json_object(text)

async def model_chat(message, history=None):
    """Call Qwen for normal text conversation through DashScope compatible API."""
    messages=[{'role':'system','content':'你是企业单据与销售数据助手。请用中文简洁回答；没有数据库证据时明确说明，不要编造订单数据。'}]
    if history:
        messages.extend(history[-8:])
    messages.append({'role':'user','content':message})
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as c:
        r=await c.post(settings.model_base_url+'/chat/completions',headers={'Authorization':'Bearer '+settings.model_api_key},json={'model':settings.model_name,'messages':messages,'temperature':0.2,'max_tokens':1024,'enable_thinking':settings.model_enable_thinking})
        r.raise_for_status()
        body=r.json(); choice=(body.get('choices') or [{}])[0]
        return (choice.get('message') or {}).get('content','').strip()

@app.get('/health')
def health():
    database='ok'
    try:
        with db() as c: c.execute('SELECT 1')
    except Exception: database='unavailable'
    return {'status':'ok' if database=='ok' else 'degraded','agent':'ok','database':database,'model':'mock' if settings.agent_mode=='mock' else 'configured','version':'0.1.0'}
@app.get('/')
def index(): return FileResponse(Path(__file__).parent.parent/'frontend'/'index.html')
@app.post('/api/chat')
async def chat(message: str=Form(''), conversation_id: str|None=Form(None), client_request_id: str=Form(...), files: list[UploadFile]|None=File(None)):
    if not message.strip() and not files: raise HTTPException(400, detail={'code':'INVALID_REQUEST','message':'请输入问题或上传 PNG 图片'})
    cid=conversation_id or str(uuid.uuid4()); trace=str(uuid.uuid4())
    if files:
        if len(files)>1: raise HTTPException(400, detail={'code':'INVALID_IMAGE','message':'MVP 每次只支持一张图片'})
        f=files[0]; data=await f.read()
        if f.content_type!='image/png' or len(data)>settings.max_upload_size_mb*1024*1024: raise HTTPException(400, detail={'code':'INVALID_IMAGE','message':'仅支持不超过 10MB 的 PNG 图片'})
        sessions.setdefault(cid, []).append({'role': 'user', 'content': message or f'上传发注书：{f.filename}'})
        context = session_context.setdefault(cid, {})
        context.pop('pending_extraction', None)
        context.pop('pending_import_result', None)
        context.pop('pending_import_key', None)
        Path(settings.upload_dir).mkdir(parents=True,exist_ok=True); (Path(settings.upload_dir)/Path(f.filename).name).write_bytes(data)
        try:
            extraction=mock_extract(f.filename) if settings.agent_mode=='mock' else await model_extract(data,f.filename)
            extraction.setdefault('image_name', f.filename)
            extraction_warnings=['当前为 Mock 模式'] if settings.agent_mode=='mock' else []
        except Exception as exc:
            extraction={'image_name':f.filename,'customer_company':None,'order_date':None,'source_company':None,'items':[]}
            extraction_warnings=['Qwen 返回格式无法解析，请重试或检查模型日志']
        answer = format_extraction_answer(extraction) if extraction.get('items') else '单据识别失败，请重试。'
        if extraction.get('items'):
            extraction['image_name'] = f.filename
            context['pending_extraction'] = extraction
        sessions[cid].append({'role': 'assistant', 'content': answer})
        return {'conversation_id':cid,'message_id':str(uuid.uuid4()),'trace_id':trace,'intent':'extract_document','status':'completed','answer':answer,'data':{'extraction':extraction},'chart':None,'sources':[{'type':'document','name':f.filename,'record_ids':[]}],'warnings':extraction_warnings,'created_at':datetime.now().isoformat()}
    warnings=[]
    try:
        # Import confirmation is a UI/API operation, never a model tool call.
        if classify(message) == 'import_document':
            sessions.setdefault(cid, []).append({'role': 'user', 'content': message})
            result = deterministic_agent(message, cid)
            sessions[cid].append({'role': 'assistant', 'content': result['answer']})
        elif settings.agent_mode == 'mock':
            sessions.setdefault(cid, []).append({'role': 'user', 'content': message})
            result = deterministic_agent(message, cid)
            sessions[cid].append({'role': 'assistant', 'content': result['answer']})
        else:
            result = await model_agent(message, cid)
    except Exception as exc:
        # Keep the service useful during a transient Qwen outage. The fallback
        # still executes the same allow-listed SQL/Python tools, never raw SQL.
        result = deterministic_agent(message, cid)
        warnings = [f'Qwen 工具编排不可用，已使用本地工具回退：{type(exc).__name__}']
        if not sessions.get(cid) or sessions[cid][-1].get('role') != 'assistant' or sessions[cid][-1].get('tool_calls'):
            sessions.setdefault(cid, []).append({'role': 'assistant', 'content': result['answer']})
    intent = result.get('intent', classify(message))
    return {'conversation_id':cid,'message_id':str(uuid.uuid4()),'trace_id':trace,'intent':intent,'status':'completed','answer':result['answer'],'data':{'records':result.get('records',[]),'summary':result.get('summary',{'order_count':0,'quantity':0,'total_amount':0}),'trend':result.get('trend'),'ranking':result.get('ranking'),'statistics':result.get('statistics'),'database_status':result.get('database_status'),'import_result':result.get('import_result')},'chart':None,'sources':[{'type':'database','name':'orders'}] if intent in ('query_orders','aggregate_sales','trend_analysis','company_ranking','python_statistics','database_status','import_document') else [],'warnings':warnings,'created_at':datetime.now().isoformat()}

@app.post('/api/documents/import')
def import_doc(payload: dict):
    key = payload.get('client_request_id') or str(uuid.uuid4())
    try:
        return persist_import(payload, key)
    except ValueError as exc:
        raise HTTPException(400, detail={'code': 'INVALID_DOCUMENT', 'message': str(exc)})
    except Exception:
        raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE', 'message': '数据库暂时不可用'})
