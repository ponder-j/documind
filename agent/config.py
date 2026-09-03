"""Application configuration for the document-processing chatbot service.

All values can be overridden through environment variables: pydantic-settings
maps ``MODEL_BASE_URL`` -> ``model_base_url`` automatically.  Secrets must stay
in the environment (or an ignored ``.env``), never in source code.
"""
import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Text/agent model (currently Qwen3.8-Flash via DashScope compatible API).
    # Keep the key in the DASHSCOPE_API_KEY environment variable (never commit
    # it to source).
    model_base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    model_name: str = 'qwen3.8-flash'
    model_api_key: str = ''

    # Vision extraction model.  Deliberately separate from the text agent
    # model: the locally deployed LLaMA-Factory InternVL service exposes model
    # ``vl`` on port 5003 (override with VISION_MODEL_BASE_URL when moved).
    vision_model_base_url: str = 'http://127.0.0.1:5003/v1'
    vision_model_name: str = 'vl'
    vision_model_api_key: str = ''

    model_timeout_seconds: int = 120
    model_enable_thinking: bool = True
    agent_mode: str = 'auto'
    database_url: str = 'postgresql://postgres:postgres@127.0.0.1:5432/chatbot'
    upload_dir: str = '/workspace/team3/chatbot/data/uploads'
    max_upload_size_mb: int = 10
    allow_origins: str = 'http://localhost:8000'


# Process-wide singleton.  The rest of the package imports this instance rather
# than instantiating its own Settings so every module reads the same values.
settings = Settings()

if not settings.model_api_key:
    # Backwards-compatible alias for the text/agent model key.
    settings.model_api_key = os.getenv('DASHSCOPE_API_KEY', '')
