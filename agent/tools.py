"""Allow-listed agent tools.

The LLM only sees :data:`TOOL_SCHEMAS` and can request these tools by name;
:func:`execute_tool` is the single dispatch point and rejects anything not in
the list.  All data access stays in Python/SQL functions in :mod:`.db`, so the
model can never execute arbitrary SQL supplied by a user.
"""
import statistics

from .charts import visualize_tool
from .db import (
    _normalise_filters,
    aggregate_sales_tool,
    company_ranking_tool,
    database_status_tool,
    query_orders_dataset,
    query_orders_tool,
    trend_tool,
)
from .state import store

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
        'name': 'visualize_data',
        'description': '将订单销售数据绘制成可视化图表（PNG 图片），返回图片 URL。适用于用户要求“画图/图表/可视化/柱状图/折线图/饼图/趋势图/对比图”时。chart_type: line=折线图(趋势)、bar=柱状图(对比)、pie=饼图(占比)；group_by: year=按年度、customer_company=按顾客公司、project=按项目、source_company=按源公司；metric: quantity=销售数量(件)、total_amount=销售金额(元)，金额/销售额用 total_amount。用户说“把刚才/这批/上述记录画成图”时 scope 用 last_query，否则默认 database。',
        'parameters': {'type': 'object', 'properties': {
            'chart_type': {'type': 'string', 'enum': ['line', 'bar', 'pie'], 'description': '图表类型'},
            'group_by': {'type': 'string', 'enum': ['year', 'customer_company', 'project', 'source_company'], 'description': '分类维度'},
            'metric': {'type': 'string', 'enum': ['quantity', 'total_amount'], 'description': '绘图指标，默认 quantity'},
            'customer_company': {'type': ['string', 'null'], 'description': '客户公司全名或关键词，用于筛选'},
            'years': {'type': 'array', 'items': {'type': 'integer'}, 'description': '年份筛选，例如 [2024]'},
            'scope': {'type': 'string', 'enum': ['database', 'last_query'], 'default': 'database', 'description': 'database=按条件从数据库聚合；last_query=对上一轮查询结果绘图'},
            'top_n': {'type': 'integer', 'description': '最多显示前N组（year 维度忽略）'},
            'title': {'type': ['string', 'null'], 'description': '可选图表标题'},
        }, 'required': ['chart_type', 'group_by'], 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'database_status',
        'description': '查看数据库连接、orders 表是否存在、记录数、客户数和日期范围。',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }},
]


def _jsonable_record(record):
    """Convert datetime/date fields to ISO strings so results stay JSON-serialisable."""
    return {key: (str(value) if hasattr(value, 'isoformat') else value) for key, value in record.items()}


def python_statistics_tool(args, cid):
    """Run deterministic Python statistics over the last query or DB rows."""
    operation = args.get('operation', 'summary')
    scope = args.get('scope', 'last_query')
    context = store.context(cid)
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
            result['top_5'] = [
                {**_jsonable_record(row), 'sales_amount': amount}
                for amount, row in sorted(amounts, reverse=True, key=lambda x: x[0])[:5]
            ]
            result['bottom_5'] = [
                {**_jsonable_record(row), 'sales_amount': amount}
                for amount, row in sorted(amounts, key=lambda x: x[0])[:5]
            ]
    return result


def execute_tool(name, args, cid):
    """Dispatch only allow-listed tools and retain data for follow-up turns."""
    args = args if isinstance(args, dict) else {}
    context = store.context(cid)
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
    if name == 'visualize_data':
        result = visualize_tool(args, cid)
        context['last_chart'] = result
        return result
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
    if name == 'visualize_data' and isinstance(result, dict):
        # The model needs the URL + a compact series, not the whole payload.
        return {
            'empty': result.get('empty', False),
            'chart_type': result.get('chart_type'),
            'title': result.get('title'),
            'image_url': result.get('image_url'),
            'point_count': result.get('point_count'),
            'summary': result.get('summary'),
            'series': (result.get('series') or [])[:20],
        }
    return result
