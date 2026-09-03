"""Vision-model document extraction (PNG -> structured fields).

The locally fine-tuned InternVL is SFT'd to answer in a fixed CSV shape
(``desc,date,from,item,amount,price``); :func:`parse_csv_extraction` converts
that CSV into the structured extraction dict consumed by the UI/import flow.
"""
import base64
import csv
import re

import httpx

from .config import settings


def mock_extract(name):
    """Return a placeholder extraction used when AGENT_MODE=mock."""
    return {
        'customer_company': '待确认顾客公司',
        'order_date': None,
        'source_company': '待确认源公司',
        'items': [{'project': '待确认项目', 'quantity': 0, 'unit_price': 0}],
        'image_name': name,
    }


def _display(value, fallback='未识别'):
    """Render an extracted value for the chat answer, with a fallback label."""
    if value is None or value == '':
        return fallback
    return str(value)


def format_extraction_answer(extraction):
    """Format an extraction dict into a human-readable confirmation card text."""
    lines = [
        '已完成发注书识别，请核对以下内容：',
        f"• 顾客公司：{_display(extraction.get('customer_company'))}",
        f"• 发注日：{_display(extraction.get('order_date'))}",
        f"• 源公司：{_display(extraction.get('source_company'))}",
    ]
    items = extraction.get('items') or []
    if items:
        lines.append('• 明细：')
        for i, item in enumerate(items, 1):
            lines.append(
                f"  {i}. {_display(item.get('project'))}，"
                f"数量 {_display(item.get('quantity'))}，"
                f"单价 {_display(item.get('unit_price'))}"
            )
    else:
        lines.append('• 明细：未识别到项目')
    lines.append('确认无误后即可导入数据库。')
    return '\n'.join(lines)


_VISION_SYSTEM = (
    '你是企业单据关键信息提取助手。根据图片逐行提取信息，只输出指定CSV，'
    '不添加Markdown或解释，不猜测图片中不可见的内容。'
)

_VISION_PROMPT = (
    '提取这张请求书的全部商品明细，输出标准CSV。\n'
    '第一行必须是：desc,date,from,item,amount,price\n'
    '字段含义：desc=顾客公司（买方）；date=单据日期；from=源公司（卖方）；item=商品名称；'
    'amount=该行商品数量（不是金额）；price=该行商品单价。\n'
    '日期规则：右上角有两个并列日期时取左侧可变日期；其他情况取请求日/請求日，不要取支払期限。'
    '日期统一为YYYY-MM-DD。\n'
    '数量使用整数；单价不带货币符号和千位逗号，保留两位小数。'
    '保留商品行的原始顺序及重复行，不合并同名商品；公司和商品名称保留原语言。'
)

_CSV_HEADER = ('desc', 'date', 'from', 'item', 'amount', 'price')


def parse_csv_extraction(text):
    """Parse the CSV answer the locally fine-tuned InternVL is trained to emit.

    The model was SFT'd on ShareGPT samples whose target is a standard CSV
    (header: desc,date,from,item,amount,price).  Asking it for unrelated JSON
    makes text fields (item/desc/from) collapse to null, so the agent must
    request the CSV format and convert it here into the structured extraction
    dict used by the UI and import flow.
    """
    if isinstance(text, list):
        text = ''.join((x.get('text', '') if isinstance(x, dict) else str(x)) for x in text)
    text = str(text or '').strip()
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S | re.I)
    text = re.sub(r'```(?:csv)?', '', text, flags=re.I).replace('```', '').strip()
    if not text:
        raise ValueError('模型未返回 CSV')
    lines = [line for line in text.splitlines() if line.strip()]
    start = 0
    for i, line in enumerate(lines):
        cells = [cell.strip().strip('"').casefold() for cell in line.split(',')]
        if cells[:6] == list(_CSV_HEADER):
            start = i + 1
            break
    rows = [row[:6] for row in csv.reader(lines[start:]) if len(row) >= 6]
    if not rows:
        raise ValueError('模型 CSV 中没有可用的明细行')
    extraction = {
        'customer_company': next((row[0].strip() for row in rows if row[0].strip()), None),
        'order_date': next((row[1].strip() for row in rows if row[1].strip()), None),
        'source_company': next((row[2].strip() for row in rows if row[2].strip()), None),
        'items': [
            {
                'project': row[3].strip() or None,
                'quantity': row[4].strip() or None,
                'unit_price': row[5].strip() or None,
            }
            for row in rows
        ],
    }
    if not any(item['project'] or item['quantity'] for item in extraction['items']):
        raise ValueError('模型 CSV 中没有可用的商品明细')
    return extraction


async def model_extract(data, name):
    """Send a PNG to the vision model and return a structured extraction."""
    content = [
        {'type': 'text', 'text': _VISION_PROMPT},
        {
            'type': 'image_url',
            'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode()},
        },
    ]
    headers = {}
    if settings.vision_model_api_key:
        headers['Authorization'] = 'Bearer ' + settings.vision_model_api_key
    payload = {
        'model': settings.vision_model_name,
        'messages': [
            {'role': 'system', 'content': _VISION_SYSTEM},
            {'role': 'user', 'content': content},
        ],
        'temperature': 0,
        'max_tokens': 2048,
        'enable_thinking': False,
    }
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
        response = await client.post(
            settings.vision_model_base_url.rstrip('/') + '/chat/completions',
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        text = response.json()['choices'][0]['message'].get('content', '')
        return parse_csv_extraction(text)
