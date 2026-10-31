"""旧课程兼容入口。新代码请阅读 app/multi_agent/。"""
from app.multi_agent.runners import DeepSeekRunner, DemoRunner
from app.multi_agent.tools import summarize_records, role_tools
from app.multi_agent.prompts import ROLE_INSTRUCTIONS
