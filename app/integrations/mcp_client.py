"""固定本地 MCP 服务的只读白名单客户端；第一阶段按批次建立会话。"""
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from app.config import PROJECT_ROOT

ALLOWED = {'weather', 'venues', 'search', 'ingredients', 'outing', 'restaurants'}


async def _batch(requests):
    # 保留运行时必要环境，不向工具子进程传递模型 Key 等无关密钥。
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'PYTHONIOENCODING', 'TAVILY_API_KEY', 'AMAP_API_KEY'}}
    params = StdioServerParameters(command=sys.executable, args=['-m', 'app.integrations.mcp_server'],
                                   cwd=str(PROJECT_ROOT), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            catalog = await session.list_tools()
            available = {tool.name for tool in catalog.tools} & ALLOWED
            results = []
            for item in requests[:4]:
                name = item['name']
                if name not in available:
                    results.append({'tool': name, 'status': 'unavailable', 'message': '工具未注册。'})
                    continue
                response = await session.call_tool(name, arguments=item['arguments'])
                chunks = [c.text for c in response.content if getattr(c, 'type', None) == 'text']
                try:
                    body = json.loads('\n'.join(chunks))
                except (ValueError, TypeError):
                    body = {'status': 'unavailable', 'message': '工具返回格式无法解析。'}
                if not isinstance(body, dict) or response.isError:
                    body = {'status': 'unavailable', 'message': 'MCP 工具执行失败。'}
                results.append({'tool': name, **body})
            return results


def call_tools(requests):
    try:
        return asyncio.run(asyncio.wait_for(_batch(requests), timeout=100))
    except Exception:
        return [{'tool': item['name'], 'status': 'unavailable', 'message': 'MCP 会话失败或超时。'} for item in requests]
