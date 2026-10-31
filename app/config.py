"""统一本地配置。只加载固定项目根目录，环境变量优先，密钥不参与 repr。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env', override=False, encoding='utf-8-sig')


@dataclass(frozen=True)
class Settings:
    tavily_api_key: str = field(default_factory=lambda: os.getenv('TAVILY_API_KEY', '').strip(), repr=False)
    amap_api_key: str = field(default_factory=lambda: os.getenv('AMAP_API_KEY', '').strip(), repr=False)
    deepseek_api_key: str = field(default_factory=lambda: os.getenv('DEEPSEEK_API_KEY', '').strip(), repr=False)
    deepseek_model: str = field(default_factory=lambda: os.getenv('DEEPSEEK_MODEL', 'deepseek-flash').strip())
    deepseek_base_url: str = field(default_factory=lambda: os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').strip())
    calendar_ics_url: str = field(default_factory=lambda: os.getenv('HENGBAN_CALENDAR_ICS_URL', '').strip(), repr=False)
    event_bridge_token: str = field(default_factory=lambda: os.getenv('HENGBAN_EVENT_BRIDGE_TOKEN', '').strip(), repr=False)
    notification_webhook_url: str = field(default_factory=lambda: os.getenv('HENGBAN_NOTIFICATION_WEBHOOK_URL','').strip(),repr=False)
    notification_webhook_token: str = field(default_factory=lambda: os.getenv('HENGBAN_NOTIFICATION_WEBHOOK_TOKEN','').strip(),repr=False)
    port: int = field(default_factory=lambda: int(os.getenv('HENGBAN_PORT', '8000')))
    host: str = field(default_factory=lambda: os.getenv('SIMON_HOST','127.0.0.1').strip())
    ssl_certfile: str = field(default_factory=lambda: os.getenv('SIMON_SSL_CERTFILE','').strip(),repr=False)
    ssl_keyfile: str = field(default_factory=lambda: os.getenv('SIMON_SSL_KEYFILE','').strip(),repr=False)
    # 角色执行预算统一配置。
    context_budget_units: int = 16000
    request_budget_units: int = 60000
    model_timeout_seconds: int = 45
    agent_max_turns: int = 6
    agent_max_tool_calls: int = 10
    agent_max_retrieval_calls: int = 3
    agent_history_max_days: int = 90
    tool_result_budget_units: int = 8000


settings = Settings()
