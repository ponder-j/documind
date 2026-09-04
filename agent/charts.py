"""Matplotlib chart generation for the order-data agent.

The ``visualize_data`` tool and the deterministic fallback both route here.
Data are aggregated in Python - either from a grouped PostgreSQL query or from
the conversation's last query rows - then rendered to a PNG under
:data:`~agent.config.Settings.chart_dir` and served by the HTTP layer at
``/api/charts/<filename>``.

The module is deliberately read-only w.r.t. the database: it only *reads*
orders through :mod:`agent.db` helpers and writes chart images to the
configured chart directory.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

import matplotlib

matplotlib.use('Agg')

import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from .config import settings  # noqa: E402
from .db import _normalise_filters, group_sales_tool  # noqa: E402
from .intent import extract_filters  # noqa: E402
from .state import store  # noqa: E402

logger = logging.getLogger(__name__)

CHART_TYPES = {'line', 'bar', 'pie'}

# Chinese display names / units for the axes, titles and captions.
GROUP_DIM_LABELS = {
    'year': '年度',
    'customer_company': '顾客公司',
    'source_company': '源公司',
    'project': '项目',
}
METRIC_LABELS = {'quantity': '销售数量', 'total_amount': '销售金额'}
METRIC_UNITS = {'quantity': '件', 'total_amount': '元'}

# Candidate CJK-capable font files (checked in priority order).  matplotlib's
# bundled DejaVu family has no CJK glyphs, so without one of these Chinese
# labels would render as empty boxes.
_FONT_FILES = (
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',                      # Ubuntu WenQuanYi
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',           # Ubuntu noto-cjk
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',                # Arch noto-cjk
    '/usr/share/fonts/truetype/arphic/uming.ttc',                       # Ubuntu AR PL UMing
    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',        # older Ubuntu
    '/System/Library/Fonts/PingFang.ttc',                               # macOS
    '/System/Library/Fonts/STHeiti Light.ttc',                          # macOS
    'C:/Windows/Fonts/msyh.ttc',                                        # Windows YaHei
    'C:/Windows/Fonts/simhei.ttf',                                      # Windows SimHei
)
_PREFERRED_FAMILIES = (
    'WenQuanYi Zen Hei',
    'Noto Sans CJK SC',
    'Source Han Sans SC',
    'Microsoft YaHei',
    'SimHei',
    'PingFang SC',
    'Heiti SC',
    'Droid Sans Fallback',
    'AR PL UMing CN',
)


def _setup_cjk_font() -> None:
    """Register the first CJK font found and make matplotlib use it."""
    added = []
    for path in _FONT_FILES:
        if not os.path.exists(path):
            continue
        try:
            fm.fontManager.addfont(path)
            added.append(path)
        except Exception as exc:  # noqa: BLE001 - a bad font must not kill the service
            logger.warning('failed to register font %s: %s', path, exc)
    families, seen = [], set()
    for entry in fm.fontManager.ttflist:
        if entry.fname in added and entry.name not in seen:
            seen.add(entry.name)
            families.append(entry.name)
    chosen = next((name for name in _PREFERRED_FAMILIES if name in families), families[0] if families else None)
    matplotlib.rcParams['axes.unicode_minus'] = False
    if chosen:
        matplotlib.rcParams['font.family'] = 'sans-serif'
        matplotlib.rcParams['font.sans-serif'] = [chosen, 'DejaVu Sans']
        logger.info('matplotlib CJK font: %s', chosen)
    else:
        logger.warning('no CJK font found; Chinese chart labels may render as boxes')


_setup_cjk_font()

# Follow-up words that make the deterministic path visualise the *previous*
# query result instead of re-querying the whole database.
_FOLLOWUP_WORDS = ('这批', '刚才', '上述', '上面', '上次', '这堆', '上轮', '这些', '那些')


def _chart_dir() -> Path:
    path = Path(settings.chart_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old_charts(keep_seconds: int = 7 * 24 * 3600) -> None:
    """Best-effort removal of chart PNGs older than ``keep_seconds``."""
    try:
        now = time.time()
        for child in _chart_dir().iterdir():
            if child.is_file() and child.name.endswith('.png'):
                try:
                    if now - child.stat().st_mtime > keep_seconds:
                        child.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


def _group_records(records, group_by):
    """Aggregate in-memory order rows (from ``last_records``) by a dimension."""
    groups = {}
    for record in records or []:
        if group_by == 'year':
            raw = record.get('order_date')
            label = str(raw)[:4] if raw else '未知年份'
        elif group_by == 'customer_company':
            label = (record.get('customer_company') or '').strip() or '未标注'
        elif group_by == 'source_company':
            label = (record.get('source_company') or '').strip() or '未标注'
        elif group_by == 'project':
            label = (record.get('project') or '').strip() or '未标注'
        else:
            label = '全部'
        bucket = groups.setdefault(label, {'label': label, 'quantity': 0.0, 'total_amount': 0.0, 'order_count': 0})
        quantity = float(record.get('quantity') or 0)
        amount = record.get('total_amount')
        if amount is None:
            amount = quantity * float(record.get('unit_price') or 0)
        bucket['quantity'] += quantity
        bucket['total_amount'] += float(amount or 0)
        bucket['order_count'] += 1
    return list(groups.values())


def _sort_rows(rows, metric, chart_type, top_n):
    """Order rows for display: chronological for line, metric-desc for others."""
    if chart_type == 'line':
        def _year_key(row):
            try:
                return float(row['label'])
            except (TypeError, ValueError):
                return float('inf')

        return sorted(rows, key=_year_key)
    ordered = sorted(rows, key=lambda row: float(row.get(metric) or 0), reverse=True)
    if top_n and len(ordered) > top_n:
        ordered = ordered[:top_n]
    return ordered


def _pie_rows(rows, metric):
    """Keep a pie readable: top 7 slices + an aggregated '其他' slice."""
    ordered = sorted(rows, key=lambda row: float(row.get(metric) or 0), reverse=True)
    if len(ordered) <= 8:
        return ordered
    head, tail = ordered[:7], ordered[7:]
    head.append({
        'label': '其他',
        'order_count': sum(row['order_count'] for row in tail),
        'quantity': sum(row['quantity'] for row in tail),
        'total_amount': sum(row['total_amount'] for row in tail),
    })
    return head


def _default_title(rows, group_by, metric, chart_type):
    dimension = GROUP_DIM_LABELS.get(group_by, group_by)
    metric_label = METRIC_LABELS.get(metric, metric)
    kind = {'line': '趋势', 'bar': '对比', 'pie': '占比'}.get(chart_type, '分布')
    if group_by == 'year':
        return f'{metric_label}{kind}图（按{dimension}）'
    return f'{metric_label}{kind}图（按{dimension}）'


def _render(rows, *, chart_type, group_by, metric, title):
    """Render one PNG from already-aggregated rows and return chart metadata."""
    chart_type = chart_type if chart_type in CHART_TYPES else 'bar'
    group_by = group_by if group_by in GROUP_DIM_LABELS else 'year'
    metric = metric if metric in METRIC_LABELS else 'quantity'
    title = str(title or '').strip() or _default_title(rows, group_by, metric, chart_type)
    unit = METRIC_UNITS[metric]
    dim_label = GROUP_DIM_LABELS[group_by]

    if chart_type == 'pie':
        rows = _pie_rows(rows, metric)
    else:
        rows = _sort_rows(rows, metric, chart_type, top_n=None if chart_type == 'line' else 20)

    labels = [row['label'] for row in rows]
    values = [float(row.get(metric) or 0) for row in rows]
    max_label_len = max((len(str(label)) for label in labels), default=0)

    if chart_type == 'pie':
        fig, ax = plt.subplots(figsize=(8.2, 5.4), dpi=150)
        ax.pie(
            values,
            labels=labels,
            autopct='%1.1f%%',
            startangle=90,
            counterclock=False,
            colors=plt.get_cmap('Set3').colors,
            textprops={'fontsize': 9},
            wedgeprops={'edgecolor': 'white', 'linewidth': 1},
        )
        ax.axis('equal')
        ax.set_title(title, fontsize=14, pad=16)
    elif chart_type == 'line':
        fig, ax = plt.subplots(figsize=(max(8, 0.9 * len(labels) + 4), 5.4), dpi=150)
        x = list(range(len(values)))
        ax.plot(x, values, marker='o', linewidth=2.2, color='#5267e8')
        ax.fill_between(x, values, color='#5267e8', alpha=0.12)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45 if max_label_len > 4 else 0, ha='right', fontsize=9)
        ax.set_xlabel(dim_label)
        ax.set_ylabel(f'{METRIC_LABELS[metric]}（{unit}）')
        ax.grid(axis='y', linestyle='--', alpha=0.4)
        for xi, value in zip(x, values):
            ax.annotate(
                f'{value:,.0f}', (xi, value),
                textcoords='offset points', xytext=(0, 8),
                ha='center', fontsize=8, color='#3a4a8f',
            )
        ax.set_title(title, fontsize=14, pad=14)
    elif max_label_len > 8 or len(labels) > 8:
        # Horizontal bars keep long company/project names readable.
        fig, ax = plt.subplots(figsize=(9.5, max(4.4, 0.42 * len(labels) + 2.2)), dpi=150)
        y = list(range(len(values)))
        ax.barh(y, values, color='#5267e8', height=0.62)
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel(f'{METRIC_LABELS[metric]}（{unit}）')
        ax.grid(axis='x', linestyle='--', alpha=0.4)
        ax.invert_yaxis()
        for yi, value in zip(y, values):
            ax.annotate(
                f'{value:,.0f}', (value, yi),
                textcoords='offset points', xytext=(3, 0),
                va='center', fontsize=8, color='#3a4a8f',
            )
        ax.set_title(title, fontsize=14, pad=14)
    else:
        fig, ax = plt.subplots(figsize=(max(7, 0.8 * len(labels) + 4), 5.4), dpi=150)
        x = list(range(len(values)))
        ax.bar(x, values, color='#5267e8', width=0.6)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=10)
        ax.set_xlabel(dim_label)
        ax.set_ylabel(f'{METRIC_LABELS[metric]}（{unit}）')
        ax.grid(axis='y', linestyle='--', alpha=0.4)
        for xi, value in zip(x, values):
            ax.annotate(
                f'{value:,.0f}', (xi, value),
                textcoords='offset points', xytext=(0, 5),
                ha='center', fontsize=8, color='#3a4a8f',
            )
        ax.set_title(title, fontsize=14, pad=14)

    fig.tight_layout()
    filename = f'chart_{uuid.uuid4().hex[:12]}.png'
    output = _chart_dir() / filename
    try:
        fig.savefig(output, format='png', bbox_inches='tight', facecolor='white')
    finally:
        plt.close(fig)
    _cleanup_old_charts()
    return {
        'chart_id': filename,
        'chart_type': chart_type,
        'group_by': group_by,
        'metric': metric,
        'title': title,
        'image_url': settings.chart_url_prefix + '/' + filename,
        'point_count': len(rows),
        'summary': {
            'dimension_label': dim_label,
            'metric_label': METRIC_LABELS[metric],
            'unit': unit,
            'total': round(sum(values), 2),
        },
        'series': [{'label': row['label'], 'value': round(float(row.get(metric) or 0), 2)} for row in rows],
    }


def visualize_tool(args, cid=None):
    """Execute a chart request (tool arguments -> chart metadata).

    ``scope='last_query'`` charts the conversation's previous query rows;
    otherwise data are read through the grouped DB helper.  Never raises for
    bad user data; returns ``{'empty': True, ...}`` when there is nothing to
    draw.
    """
    args = dict(args or {})
    chart_type = args.get('chart_type') if args.get('chart_type') in CHART_TYPES else 'bar'
    group_by = args.get('group_by') if args.get('group_by') in GROUP_DIM_LABELS else 'year'
    metric = args.get('metric') if args.get('metric') in METRIC_LABELS else 'quantity'
    scope = args.get('scope') or 'database'
    top_n = args.get('top_n')
    try:
        top_n = int(top_n) if top_n not in (None, '', 'null') else None
    except (TypeError, ValueError):
        top_n = None

    records = None
    if scope == 'last_query' and cid is not None:
        records = store.context(cid).get('last_records')
    if records is not None:
        rows = _group_records(records, group_by)
    else:
        rows = group_sales_tool(_normalise_filters(args), group_by)

    if not rows or all(float(row.get(metric) or 0) == 0 for row in rows):
        return {
            'chart_type': chart_type,
            'group_by': group_by,
            'metric': metric,
            'empty': True,
            'message': '没有可用于绘图的订单数据，请先查询数据或调整筛选条件。',
        }
    return _render(rows, chart_type=chart_type, group_by=group_by, metric=metric, title=args.get('title'))


def _request_group_by(message):
    """Pick the chart category dimension from a natural-language request."""
    if '源公司' in message:
        return 'source_company'
    if '项目' in message:
        return 'project'
    if any(word in message for word in ('趋势', '走势', '年度', '年份', '各年', '按年', '逐年')):
        return 'year'
    if any(word in message for word in ('公司', '客户')):
        return 'customer_company'
    return 'year'


def _request_chart_type(message):
    """Infer line/bar/pie from words such as 饼图/柱状图/趋势."""
    if any(word in message for word in ('饼', '占比', '构成', '结构')):
        return 'pie'
    if any(word in message for word in ('柱', '条形', '条图', '对比', '排行榜', '榜单', '排名', '比较')):
        return 'bar'
    return 'line'


def _is_followup(message):
    return any(word in message for word in _FOLLOWUP_WORDS)


def visualize_from_message(message, cid):
    """Deterministic-path entry point: derive chart args from free text."""
    filters = extract_filters(message)
    metric = (
        'total_amount'
        if any(word in message for word in ('金额', '销售额', '交易额', '总金额', '总价'))
        else 'quantity'
    )
    group_by = _request_group_by(message)
    chart_type = _request_chart_type(message)
    args = {
        'chart_type': chart_type,
        'group_by': group_by,
        'metric': metric,
        'years': filters['years'],
        'customer_company': filters['customer_company'],
    }
    if _is_followup(message) and store.context(cid).get('last_records') is not None:
        # Follow-ups ("把刚才/这批/上述记录画成图") chart the previous full
        # query result instead of re-querying the whole database.
        args['scope'] = 'last_query'
    match = re.search(r'前\s*(\d+)', message)
    if match and group_by != 'year':
        args['top_n'] = int(match.group(1))
    return visualize_tool(args, cid)
