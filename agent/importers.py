"""Validation and persistence for user-confirmed document imports.

Numbers/dates coming out of a vision model or a human-edited card are free
text; this module coerces them into database-safe values and rejects anything
malformed with a user-facing ``ValueError``.  Imports are idempotent per
``client_request_id`` so a retried HTTP request cannot double-insert rows.
"""
import math
import re
import uuid
from datetime import datetime

from .db import db
from .state import store


def _number(value, field, item_index):
    """Convert OCR/model numeric text to a database-safe float."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f'第 {item_index} 条明细的{field}不是有效数字')
    if isinstance(value, str):
        # Normalise Chinese/wide separators and currency symbols, then drop
        # thousands separators so "1,234.50" / "￥1,234" parse correctly.
        value = value.strip().translate(str.maketrans('，,￥¥　', ',,,, ')).replace(',', '')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'第 {item_index} 条明细的{field}不是有效数字') from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'第 {item_index} 条明细的{field}必须是非负数字')
    return number


def _order_date(value):
    """Normalise a date string to ``YYYY-MM-DD`` or return None."""
    if value is None or not str(value).strip():
        return None
    value = str(value).strip().replace('/', '-').replace('.', '-')
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', value)
    if not match:
        raise ValueError('发注日格式应为 YYYY-MM-DD')
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        raise ValueError('发注日不是有效日期') from None


def normalise_import_payload(payload):
    """Validate and normalise one import payload into DB-ready rows."""
    if not isinstance(payload, dict):
        raise ValueError('导入数据格式错误')
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('没有可导入的明细')
    out = {
        'image_name': str(payload.get('image_name') or ''),
        'customer_company': payload.get('customer_company'),
        'order_date': _order_date(payload.get('order_date')),
        'source_company': payload.get('source_company'),
        'items': [],
    }
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index} 条明细格式错误')
        out['items'].append({
            'project': item.get('project'),
            'quantity': _number(item.get('quantity'), '数量', index),
            'unit_price': _number(item.get('unit_price'), '单价', index),
        })
    return out


def persist_import(payload, key):
    """Insert one extracted document idempotently and return its IDs."""
    cached = store.seen_import(key)
    if cached:
        return cached
    payload = normalise_import_payload(payload)
    items = payload['items']
    ids = []
    with db() as conn:
        for item in items:
            quantity = item.get('quantity')
            unit_price = item.get('unit_price')
            total = (
                (quantity or 0) * (unit_price or 0)
                if quantity is not None and unit_price is not None
                else None
            )
            row = conn.execute(
                'INSERT INTO orders(image_name, customer_company, order_date, source_company, '
                'project, quantity, unit_price, total_amount) '
                'VALUES(%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                (
                    payload.get('image_name', ''),
                    payload.get('customer_company'),
                    payload.get('order_date'),
                    payload.get('source_company'),
                    item.get('project'),
                    quantity,
                    unit_price,
                    total,
                ),
            ).fetchone()
            ids.append(row[0])
    out = {
        'status': 'imported',
        'document_id': str(uuid.uuid4()),
        'record_ids': ids,
        'imported_count': len(ids),
        'trace_id': str(uuid.uuid4()),
    }
    store.mark_imported(key, out)
    return out
