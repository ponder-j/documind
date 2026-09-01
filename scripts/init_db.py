"""Initialize PostgreSQL orders table and import output_label.xlsx."""
import os, argparse, re, csv
from pathlib import Path
import openpyxl
import psycopg
from datetime import date

DB=os.getenv('DATABASE_URL','postgresql://postgres:postgres@127.0.0.1:5432/chatbot')
XLSX=Path(os.getenv('LABEL_FILE','materials/班级7用数据/output_label.xlsx'))
parser=argparse.ArgumentParser(); parser.add_argument('--reset', action='store_true', help='replace previously imported orders'); args=parser.parse_args()
def number(value):
    if value in (None, ''): return None
    cleaned=re.sub(r'[^0-9.\-]', '', str(value))
    return float(cleaned) if cleaned else None
def date_value(value):
    if value in (None, ''): return None
    if hasattr(value, 'date'): return value.date()
    s=str(value).strip()
    m=re.search(r'(20\d{2})\D+(\d{1,2})\D+(\d{1,2})', s)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
with psycopg.connect(DB) as c:
    c.execute('''CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY,image_name TEXT NOT NULL,customer_company TEXT,order_date DATE,source_company TEXT,project TEXT,quantity DOUBLE PRECISION,unit_price DOUBLE PRECISION,total_amount DOUBLE PRECISION,created_at TIMESTAMPTZ DEFAULT now())''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_orders_customer_date ON orders(customer_company, order_date)')
    if args.reset: c.execute('TRUNCATE TABLE orders RESTART IDENTITY')
    elif c.execute('SELECT COUNT(*) FROM orders').fetchone()[0] > 0:
        print('orders table already contains data; skip import (use --reset to replace)')
        raise SystemExit(0)
    if XLSX.suffix.lower() == '.csv':
        with XLSX.open(newline='', encoding='utf-8-sig') as f:
            rows=list(csv.reader(f))
    else:
        sheet=openpyxl.load_workbook(XLSX,read_only=True,data_only=True).active
        rows=list(sheet.iter_rows(values_only=True))
    header=[str(x).strip() if x else '' for x in rows[0]]
    aliases={'图像名':'image_name','顾客公司':'customer_company','发注日':'order_date','源公司':'source_company','项目':'project','数量':'quantity','单价':'unit_price'}
    idx={aliases.get(h,h):i for i,h in enumerate(header)}
    for row in rows[1:]:
        g=lambda k: row[idx[k]] if k in idx and idx[k]<len(row) else None
        q=number(g('quantity')); p=number(g('unit_price'))
        image=str(g('image_name') or '').replace('output_image\\','')
        c.execute('INSERT INTO orders(image_name,customer_company,order_date,source_company,project,quantity,unit_price,total_amount) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(image,g('customer_company'),date_value(g('order_date')),g('source_company'),g('project'),q,p,q*p if q is not None and p is not None else None))
print('database initialized and data imported')
