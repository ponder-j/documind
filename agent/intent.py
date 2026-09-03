"""Rule-based intent classification and message -> query-filter extraction.

These heuristics power the deterministic (no-model) fallback path and provide
an intent label when the LLM agent answers without calling any tool.
"""
import re

from .db import db


def classify(msg):
    """Classify a user message into one of the supported intent labels."""
    if any(k in msg for k in ('导入数据库', '确认导入', '写入数据库', '保存到数据库', '入库')):
        return 'import_document'
    if any(k in msg for k in ('当前数据库', '数据库情况', '数据库状态', '表结构', '字段范围', '数据量', '数据总量', '有多少条数据', '有多少条记录')):
        return 'database_status'
    if any(k in msg for k in ('前10', '前 10', '前5', '前 5', '前几名', '排名', '排行', '榜单', '排个名', '从小到大', '从大到小', '升序', '降序')) and any(k in msg for k in ('公司', '客户', '交易额', '销售额', '销售量', '数量')):
        return 'company_ranking'
    if any(k in msg for k in ('趋势', '变化', '走势')):
        return 'trend_analysis'
    if any(k in msg for k in ('最高', '最低', '最大', '最小', '平均', '均值', '排序', '排名')):
        return 'python_statistics'
    if any(k in msg for k in ('多少件', '数量', '总金额', '总额', '交易额', '销售额')):
        return 'aggregate_sales'
    if any(k in msg for k in ('查询', '销售数据', '订单')):
        return 'query_orders'
    return 'general_chat'


def extract_filters(message):
    """Pull years and a customer-company keyword out of a natural-language query."""
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
        with db() as conn:
            names = [
                row[0]
                for row in conn.execute(
                    'SELECT DISTINCT customer_company FROM orders WHERE customer_company IS NOT NULL'
                ).fetchall()
            ]
        matches = [name for name in names if name and name in cleaned]
        if matches:
            company = max(matches, key=len)
    except Exception:
        # Filtering should still work when the database is temporarily down;
        # the SQL tool will report the actual connection error to the caller.
        pass
    if company is None:
        # Fallback for a company not yet present in the DB (useful for diagnostics).
        # Only accept strong company suffixes; a bare "公司" would otherwise match
        # generic phrases such as "所有公司/各公司" and poison the SQL filter.
        m = re.search(r'([^\s，。,.、！？?]+?(?:股份有限公司|有限责任公司|有限公司|株式会社))', cleaned)
        if m:
            company = m.group(1).strip()
    return {'years': years, 'customer_company': company}
