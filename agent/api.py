"""FastAPI HTTP layer for the document-processing chatbot (routes only).

Business logic lives in the sibling modules (``agents``, ``vision``,
``importers``, ``db``, ``intent``, ``tools``); this file only wires routes to
them.  Keeping it thin means the deployed entry point
``uvicorn agent.main:app`` never changes when internals are refactored.
"""
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .agents import deterministic_agent, model_agent
from .config import settings
from .db import db, init_db
from .importers import persist_import
from .intent import classify
from .state import store
from .vision import format_extraction_answer, mock_extract, model_extract

app = FastAPI(title='企业文档处理对话智能体', version='0.1.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.allow_origins.split(',')],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Ensure the orders table exists at startup (safe when the DB is unreachable;
# init_db logs and swallows connection failures).
init_db()

# Intents whose answer draws on database rows (used to tag response sources).
DB_INTENTS = (
    'query_orders',
    'aggregate_sales',
    'trend_analysis',
    'company_ranking',
    'python_statistics',
    'database_status',
    'import_document',
)


def _save_upload(data: bytes, filename: str) -> None:
    """Persist an uploaded PNG under the configured upload directory."""
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / Path(filename).name).write_bytes(data)


def _run_deterministic(cid: str, message: str) -> dict:
    """Record the turn and answer with the local rule-based agent."""
    store.append(cid, {'role': 'user', 'content': message})
    result = deterministic_agent(message, cid)
    store.append(cid, {'role': 'assistant', 'content': result['answer']})
    return result


async def _handle_document_upload(cid: str, message: str, upload: UploadFile) -> dict:
    """Run one uploaded PNG through the vision extraction pipeline."""
    data = await upload.read()
    if upload.content_type != 'image/png' or len(data) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(
            400,
            detail={'code': 'INVALID_IMAGE', 'message': '仅支持不超过 10MB 的 PNG 图片'},
        )
    store.append(cid, {'role': 'user', 'content': message or f'上传发注书：{upload.filename}'})
    context = store.context(cid)
    for key in ('pending_extraction', 'pending_import_result', 'pending_import_key'):
        context.pop(key, None)
    _save_upload(data, upload.filename)
    try:
        if settings.agent_mode == 'mock':
            extraction = mock_extract(upload.filename)
            extraction_warnings = ['当前为 Mock 模式']
        else:
            extraction = await model_extract(data, upload.filename)
            extraction_warnings = []
        extraction.setdefault('image_name', upload.filename)
    except Exception as exc:
        extraction = {
            'image_name': upload.filename,
            'customer_company': None,
            'order_date': None,
            'source_company': None,
            'items': [],
        }
        extraction_warnings = [
            '视觉模型识别失败（{}），请检查 VISION_MODEL_BASE_URL/模型服务日志后重试'.format(type(exc).__name__)
        ]
    answer = format_extraction_answer(extraction) if extraction.get('items') else '单据识别失败，请重试。'
    if extraction.get('items'):
        extraction['image_name'] = upload.filename
        context['pending_extraction'] = extraction
    store.append(cid, {'role': 'assistant', 'content': answer})
    return {
        'conversation_id': cid,
        'message_id': str(uuid.uuid4()),
        'trace_id': str(uuid.uuid4()),
        'intent': 'extract_document',
        'status': 'completed',
        'answer': answer,
        'data': {'extraction': extraction},
        'chart': None,
        'sources': [{'type': 'document', 'name': upload.filename, 'record_ids': []}],
        'warnings': extraction_warnings,
        'created_at': datetime.now().isoformat(),
    }


async def _handle_text_message(cid: str, message: str):
    """Answer a text question, preferring the LLM path with rule fallback."""
    warnings = []
    try:
        # Import confirmation is a UI/API operation, never a model tool call.
        if classify(message) == 'import_document':
            result = _run_deterministic(cid, message)
        elif settings.agent_mode == 'mock':
            result = _run_deterministic(cid, message)
        else:
            result = await model_agent(message, cid)
    except Exception as exc:
        # Keep the service useful during a transient Qwen outage. The fallback
        # still executes the same allow-listed SQL/Python tools, never raw SQL.
        result = deterministic_agent(message, cid)
        detail = str(exc)
        if len(detail) > 300:
            detail = detail[:300] + '...'
        warnings = [f'Qwen 工具编排不可用，已使用本地工具回退：{type(exc).__name__}: {detail}']
        last = store.last_message(cid)
        if last is None or last.get('role') != 'assistant' or last.get('tool_calls'):
            store.append(cid, {'role': 'assistant', 'content': result['answer']})
    return result, warnings


def _chat_response(cid: str, trace: str, intent: str, result: dict, warnings: list) -> dict:
    """Assemble the JSON envelope returned by the text-chat endpoint."""
    return {
        'conversation_id': cid,
        'message_id': str(uuid.uuid4()),
        'trace_id': trace,
        'intent': intent,
        'status': 'completed',
        'answer': result['answer'],
        'data': {
            'records': result.get('records', []),
            'summary': result.get('summary', {'order_count': 0, 'quantity': 0, 'total_amount': 0}),
            'trend': result.get('trend'),
            'ranking': result.get('ranking'),
            'statistics': result.get('statistics'),
            'database_status': result.get('database_status'),
            'import_result': result.get('import_result'),
        },
        'chart': None,
        'sources': [{'type': 'database', 'name': 'orders'}] if intent in DB_INTENTS else [],
        'warnings': warnings,
        'created_at': datetime.now().isoformat(),
    }


@app.get('/health')
def health():
    database = 'ok'
    try:
        with db() as conn:
            conn.execute('SELECT 1')
    except Exception:
        database = 'unavailable'
    return {
        'status': 'ok' if database == 'ok' else 'degraded',
        'agent': 'ok',
        'database': database,
        'model': 'mock' if settings.agent_mode == 'mock' else 'configured',
        'version': '0.1.0',
    }


@app.get('/')
def index():
    return FileResponse(Path(__file__).parent.parent / 'frontend' / 'index.html')


@app.post('/api/chat')
async def chat(
    message: str = Form(''),
    conversation_id: str | None = Form(None),
    client_request_id: str = Form(...),
    files: list[UploadFile] | None = File(None),
):
    if not message.strip() and not files:
        raise HTTPException(400, detail={'code': 'INVALID_REQUEST', 'message': '请输入问题或上传 PNG 图片'})
    cid = conversation_id or str(uuid.uuid4())
    trace = str(uuid.uuid4())
    if files:
        if len(files) > 1:
            raise HTTPException(400, detail={'code': 'INVALID_IMAGE', 'message': 'MVP 每次只支持一张图片'})
        return await _handle_document_upload(cid, message, files[0])
    result, warnings = await _handle_text_message(cid, message)
    intent = result.get('intent', classify(message))
    return _chat_response(cid, trace, intent, result, warnings)


@app.post('/api/documents/import')
def import_doc(payload: dict):
    key = payload.get('client_request_id') or str(uuid.uuid4())
    try:
        return persist_import(payload, key)
    except ValueError as exc:
        raise HTTPException(400, detail={'code': 'INVALID_DOCUMENT', 'message': str(exc)})
    except Exception:
        raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE', 'message': '数据库暂时不可用'})
