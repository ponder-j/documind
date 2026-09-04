"""Conversation agents: LLM tool-orchestration path and deterministic fallback.

Both entry points return the *same* result shape (see :func:`build_result`),
which the HTTP layer forwards to the frontend.  The deterministic path is a
pure-Python fallback used when the text model endpoint is unavailable or
rejects tool calls, so the service keeps answering from the same allow-listed
tools instead of guessing.
"""
import json
import re
import uuid

import httpx

from .charts import visualize_from_message
from .config import settings
from .db import (
    aggregate_sales_tool,
    company_ranking_tool,
    database_status_tool,
    query_orders_dataset,
    trend_tool,
)
from .intent import classify, extract_filters
from .state import store
from .tools import TOOL_SCHEMAS, execute_tool, python_statistics_tool, tool_result_for_model

# Sentinel used wherever an empty result summary is required.
EMPTY_SUMMARY = {'order_count': 0, 'quantity': 0, 'total_amount': 0}

# Tool name -> intent label for the response envelope.
TOOL_INTENTS = {
    'query_orders': 'query_orders',
    'aggregate_sales': 'aggregate_sales',
    'trend_analysis': 'trend_analysis',
    'company_ranking': 'company_ranking',
    'python_statistics': 'python_statistics',
    'visualize_data': 'data_visualization',
    'database_status': 'database_status',
}


def build_result(*, answer, intent, records=None, summary=None, trend=None,
                 ranking=None, statistics=None, database_status=None,
                 import_result=None, chart=None):
    """Assemble the canonical agent response envelope used by the HTTP layer."""
    return {
        'answer': answer,
        'intent': intent,
        'records': [] if records is None else records,
        'summary': dict(EMPTY_SUMMARY) if summary is None else summary,
        'trend': trend,
        'ranking': ranking,
        'statistics': statistics,
        'database_status': database_status,
        'import_result': import_result,
        'chart': chart,
    }


def _content_text(value):
    """Extract plain text from an OpenAI-style content (str or part list)."""
    if isinstance(value, list):
        return ''.join((x.get('text', '') if isinstance(x, dict) else str(x)) for x in value)
    return str(value or '')


def _model_summary(last_tool_name, last_tool_result, context):
    """Build the summary field for the LLM path after its last tool call."""
    if last_tool_name == 'aggregate_sales':
        return last_tool_result
    if last_tool_name == 'company_ranking' and isinstance(last_tool_result, list):
        return {
            'order_count': sum(int(r.get('order_count') or 0) for r in last_tool_result),
            'quantity': sum(float(r.get('quantity') or 0) for r in last_tool_result),
            'total_amount': sum(float(r.get('total_amount') or 0) for r in last_tool_result),
        }
    # Default: summarise the most recent query results held in context.
    meta = context.get('last_query_meta') or {}
    records = context.get('last_records') or []
    return {
        'order_count': meta.get('total_matches', len(records)),
        'quantity': meta.get('total_quantity', sum(float(r.get('quantity') or 0) for r in records)),
        'total_amount': meta.get('total_amount', sum(float(r.get('total_amount') or 0) for r in records)),
    }


async def model_agent(message, cid):
    """Let Qwen select tools, then let Python execute and explain their results."""
    if not settings.model_api_key:
        raise RuntimeError('MODEL_API_KEY/DASHSCOPE_API_KEY 未配置')
    history = store.history(cid)
    store.append(cid, {'role': 'user', 'content': message})
    system = (
        '你是企业订单数据智能体。请用中文回答，事实问题必须调用工具，不能凭空编造。'
        '涉及订单、销售、客户、年份、数量、金额或数据库状态时，先选择合适的工具。'
        '“这批/上述/刚才这些记录”等追问必须调用 python_statistics，scope 使用 last_query；'
        'python_statistics 会在服务器端用 Python 对上一轮完整结果统计。'
        'query_orders 返回全部明细及 total_matches；若用户要最高、最低、平均、排序，必须调用 python_statistics。'
        '用户要求按公司/客户分组、排名、前N名、榜单时，必须调用 company_ranking；'
        '总交易额/销售额使用 metric=total_amount，销售数量/销量使用 metric=quantity，top_n 使用用户指定的数字。'
        '不要把公司级排名误当成单笔订单最高，也不要在未调用工具时猜测数据库结果。'
        '用户要求画图/图表/可视化/柱状图/折线图/饼图/趋势图/对比图时，必须调用 visualize_data：'
        '趋势按年用 chart_type=line + group_by=year；'
        '公司/项目/源公司对比用 chart_type=bar + group_by=customer_company/project/source_company；'
        '占比用 chart_type=pie；金额/销售额用 metric=total_amount，数量/销量用 metric=quantity；'
        '“把刚才/这批/上述记录画成图”用 scope=last_query，否则默认 database。'
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
            response = await client.post(
                settings.model_base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + settings.model_api_key},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        choice = (body.get('choices') or [{}])[0]
        assistant = choice.get('message') or {}
        calls = assistant.get('tool_calls') or []
        if not calls:
            answer = _content_text(assistant.get('content')).strip()
            if not answer:
                raise RuntimeError('Qwen 未返回文本或工具调用')
            store.append(cid, {'role': 'assistant', 'content': answer})
            context = store.context(cid)
            return build_result(
                answer=answer,
                intent=TOOL_INTENTS.get(last_tool_name, classify(message)),
                records=context.get('last_records', []),
                summary=_model_summary(last_tool_name, last_tool_result, context),
                trend=last_tool_result if last_tool_name == 'trend_analysis' else None,
                ranking=last_tool_result if last_tool_name == 'company_ranking' else None,
                statistics=last_tool_result if last_tool_name == 'python_statistics' else None,
                database_status=last_tool_result if last_tool_name == 'database_status' else None,
                chart=last_tool_result if last_tool_name == 'visualize_data' else None,
            )
        store.append(
            cid,
            {
                'role': 'assistant',
                'content': _content_text(assistant.get('content')) or None,
                'tool_calls': calls,
            },
        )
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
            store.append(
                cid,
                {
                    'role': 'tool',
                    'tool_call_id': call.get('id', str(uuid.uuid4())),
                    'name': name,
                    'content': json.dumps(
                        tool_result_for_model(name, result),
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )
    raise RuntimeError('工具调用超过最大轮次')


def deterministic_agent(message, cid):
    """Safe fallback when the model endpoint is unavailable or rejects tools."""
    intent = classify(message)
    filters = extract_filters(message)
    context = store.context(cid)
    if intent == 'query_orders':
        dataset = query_orders_dataset(filters)
        context['last_records'] = dataset['records']
        context['last_query_meta'] = dataset
        context['last_query'] = filters
        answer = f"已查询到 {dataset['total_matches']} 条订单记录。"
        return build_result(
            answer=answer,
            intent=intent,
            records=dataset['records'],
            summary={
                'order_count': dataset['total_matches'],
                'quantity': sum(float(r.get('quantity') or 0) for r in dataset['records']),
                'total_amount': sum(float(r.get('total_amount') or 0) for r in dataset['records']),
            },
        )
    if intent == 'aggregate_sales':
        summary = aggregate_sales_tool(filters)
        answer = (
            f"根据数据库记录，共 {summary['order_count']} 条订单，"
            f"销售数量 {summary['quantity']:g} 件，"
            f"销售金额 {summary['total_amount']:,.2f}。"
        )
        return build_result(answer=answer, intent=intent, records=[], summary=summary)
    if intent == 'trend_analysis':
        trend = trend_tool(filters)
        if trend:
            answer = '年度销售趋势：' + '；'.join(f"{x['year']}年 {x['quantity']:g} 件" for x in trend)
        else:
            answer = '暂无可用于趋势分析的订单数据。'
        return build_result(
            answer=answer,
            intent=intent,
            records=[],
            summary={
                'order_count': len(trend),
                'quantity': sum(x['quantity'] for x in trend),
                'total_amount': sum(x['total_amount'] for x in trend),
            },
            trend=trend,
        )
    if intent == 'company_ranking':
        metric = (
            'quantity'
            if any(k in message for k in ('销售量', '销量', '销售数量', '数量'))
            and not any(k in message for k in ('交易额', '销售额', '金额'))
            else 'total_amount'
        )
        top_match = re.search(r'前\s*(\d+)\s*(?:名|家|个)', message)
        top_n = int(top_match.group(1)) if top_match else 10
        ranking = company_ranking_tool({'metric': metric, 'top_n': top_n, **filters})
        if any(k in message for k in ('从小到大', '由小到大', '由低到高', '从低到高', '升序', '少到多')):
            ranking = list(reversed(ranking))
        label = '销售数量' if metric == 'quantity' else '总交易额'
        if not ranking:
            answer = '数据库中没有可用于公司排名的订单记录。'
        else:
            metric_key = 'quantity' if metric == 'quantity' else 'total_amount'
            answer = '按{}排名前{}名：\n'.format(label, len(ranking)) + '\n'.join(
                f"{i}. {row['customer_company']}：{row[metric_key]:,.2f}"
                for i, row in enumerate(ranking, 1)
            )
        return build_result(
            answer=answer,
            intent=intent,
            records=[],
            summary={
                'order_count': sum(row['order_count'] for row in ranking),
                'quantity': sum(row['quantity'] for row in ranking),
                'total_amount': sum(row['total_amount'] for row in ranking),
            },
            ranking=ranking,
        )
    if intent == 'python_statistics':
        scope = 'last_query' if context.get('last_records') is not None else 'database'
        stats = python_statistics_tool({'operation': 'max_min', 'scope': scope, **filters}, cid)
        if not stats.get('record_count'):
            answer = '没有可供 Python 统计的订单记录。'
        else:
            answer = (
                f"已用 Python 统计 {stats['record_count']} 条记录："
                f"销售额最高为 {stats['maximum']['sales_amount']:,.2f}，"
                f"最低为 {stats['minimum']['sales_amount']:,.2f}。"
            )
        return build_result(
            answer=answer,
            intent=intent,
            records=context.get('last_records', []),
            summary={
                'order_count': stats.get('record_count', 0),
                'quantity': stats.get('quantity_sum', 0),
                'total_amount': stats.get('sales_amount_sum', 0),
            },
            statistics=stats,
        )
    if intent == 'data_visualization':
        chart = visualize_from_message(message, cid)
        if not chart or chart.get('empty'):
            message_text = chart.get('message') if isinstance(chart, dict) else ''
            return build_result(
                answer=message_text or '没有可用于绘图的订单数据，请先查询数据或换一个筛选条件。',
                intent=intent,
                records=[],
            )
        summary = chart['summary']
        answer = (
            f"已生成「{chart['title']}」：按{summary['dimension_label']}共 {chart['point_count']} 组，"
            f"合计 {summary['metric_label']} {summary['total']:,.2f} {summary['unit']}。"
        )
        return build_result(answer=answer, intent=intent, records=[], chart=chart)
    if intent == 'database_status':
        status = database_status_tool()
        if status.get('exists'):
            answer = (
                f"数据库连接正常，orders 表已有 {status['record_count']:,} 条订单记录，"
                f"包含 {status['company_count']} 家客户，"
                f"日期范围为 {status['date_min']} 至 {status['date_max']}。"
            )
        else:
            answer = '数据库连接正常，但尚未创建 orders 表。'
        return build_result(
            answer=answer,
            intent=intent,
            records=[],
            database_status=status,
        )
    if intent == 'import_document':
        return build_result(
            answer='请在识别结果卡片中核对或修改字段，然后点击“确认导入”。',
            intent=intent,
            records=[],
        )
    answer = '我可以识别发注书、查询订单、统计销售数据、分析趋势，并把数据画成图表。'
    return build_result(answer=answer, intent=intent, records=[])
