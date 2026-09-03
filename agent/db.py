"""PostgreSQL access layer for the ``orders`` table.

Every function in this module is read-only except :func:`init_db` (idempotent
DDL).  No user- or model-supplied string is ever concatenated into SQL here:
values always flow through psycopg parameter binding (see ``_where_clause``).
"""
import logging

import psycopg

from .config import settings

logger = logging.getLogger(__name__)

# Column order must match the SELECT list used by the query functions below.
ORDER_FIELDS = (
    'image_name',
    'customer_company',
    'order_date',
    'source_company',
    'project',
    'quantity',
    'unit_price',
    'total_amount',
)


def db():
    """Open a new psycopg connection using the configured DSN."""
    return psycopg.connect(settings.database_url)


def init_db():
    """Create the ``orders`` table if it does not exist (idempotent)."""
    try:
        with db() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    image_name TEXT NOT NULL,
                    customer_company TEXT,
                    order_date DATE,
                    source_company TEXT,
                    project TEXT,
                    quantity DOUBLE PRECISION,
                    unit_price DOUBLE PRECISION,
                    total_amount DOUBLE PRECISION,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
                '''
            )
    except Exception as exc:  # noqa: BLE001 - startup must not crash on DB outage
        logger.warning('init_db failed (service keeps running): %s', exc)


def _where_clause(filters):
    """Build a parameterised ``WHERE`` fragment from validated filters.

    Returns ``(sql_fragment, args)`` where every value is bound via psycopg
    parameters; the fragment is empty when no filter is present.
    """
    clauses, args = [], []
    if filters.get('customer_company'):
        clauses.append('customer_company ILIKE %s')
        args.append('%' + filters['customer_company'] + '%')
    if filters.get('years'):
        clauses.append('EXTRACT(YEAR FROM order_date) = ANY(%s)')
        args.append(filters['years'])
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    return where, args


def _record_dicts(rows):
    """Map raw SELECT rows to dicts keyed by :data:`ORDER_FIELDS`."""
    return [dict(zip(ORDER_FIELDS, row)) for row in rows]


def query_orders_dataset(filters):
    """Return matching orders plus row count and quantity/amount totals."""
    where, args = _where_clause(filters)
    with db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM orders' + where, args).fetchone()[0]
        totals = conn.execute(
            'SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(total_amount), 0) FROM orders' + where,
            args,
        ).fetchone()
        rows = conn.execute(
            'SELECT image_name, customer_company, order_date, source_company, project, '
            'quantity, unit_price, total_amount FROM orders' + where
            + ' ORDER BY order_date DESC NULLS LAST',
            args,
        ).fetchall()
    return {
        'records': _record_dicts(rows),
        'total_matches': total,
        'total_quantity': float(totals[0]),
        'total_amount': float(totals[1]),
        'returned_count': len(rows),
    }


def query_orders_tool(filters):
    """Return only the matching order records (full detail, no summary)."""
    return query_orders_dataset(filters)['records']


def aggregate_sales_tool(filters):
    """Return count, total quantity and total amount for matching orders."""
    where, args = _where_clause(filters)
    with db() as conn:
        row = conn.execute(
            'SELECT COUNT(*), COALESCE(SUM(quantity), 0), COALESCE(SUM(total_amount), 0) '
            'FROM orders' + where,
            args,
        ).fetchone()
    return {'order_count': row[0], 'quantity': float(row[1]), 'total_amount': float(row[2])}


def trend_tool(filters):
    """Return per-year quantity/amount series for matching orders."""
    where, args = _where_clause(filters)
    with db() as conn:
        rows = conn.execute(
            'SELECT EXTRACT(YEAR FROM order_date)::int, COALESCE(SUM(quantity), 0), '
            'COALESCE(SUM(total_amount), 0) FROM orders' + where + ' GROUP BY 1 ORDER BY 1',
            args,
        ).fetchall()
    return [
        {'year': row[0], 'quantity': float(row[1]), 'total_amount': float(row[2])}
        for row in rows
    ]


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
    where = (where + ' AND ' if where else ' WHERE ') + company_clause
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
    with db() as conn:
        rows = conn.execute(sql, query_args).fetchall()
    return [
        {
            'customer_company': row[0],
            'order_count': int(row[1]),
            'quantity': float(row[2] or 0),
            'total_amount': float(row[3] or 0),
        }
        for row in rows
    ]


def database_status_tool():
    """Return safe, read-only metadata so the agent can explain DB availability."""
    with db() as conn:
        table = conn.execute("SELECT to_regclass('public.orders')").fetchone()[0]
        if table is None:
            return {'connected': True, 'table': 'orders', 'exists': False, 'record_count': 0}
        record_count = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        companies = conn.execute('SELECT COUNT(DISTINCT customer_company) FROM orders').fetchone()[0]
        date_min, date_max = conn.execute('SELECT MIN(order_date), MAX(order_date) FROM orders').fetchone()
        return {
            'connected': True,
            'table': 'orders',
            'exists': True,
            'record_count': record_count,
            'company_count': companies,
            'date_min': date_min,
            'date_max': date_max,
        }


def _normalise_filters(args):
    """Sanitise model/tool arguments into a clean ``{customer_company, years}`` dict.

    Years are coerced to ints (non-numeric entries dropped) and whole-scope
    company placeholders such as ``"null"``/``"全部"`` become no filter, so
    generated strings can never leak into SQL parameter binding.
    """
    years = args.get('years') or []
    if not isinstance(years, list):
        years = [years]
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
