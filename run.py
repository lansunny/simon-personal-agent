"""PyCharm 直接运行；固定工作目录，启动成功后打开浏览器。"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.request import urlopen
import json

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    local_python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    if local_python.exists() and Path(sys.executable).resolve() != local_python.resolve():
        print('使用项目 .venv 解释器启动…', flush=True)
        return subprocess.call([str(local_python), '-B', str(ROOT / 'run.py')])
    try:
        from app.config import settings
        import uvicorn
        from app.backend.api import app
    except ImportError:
        print('缺少依赖，请在项目终端执行：.venv\\Scripts\\python.exe -m pip install -r requirements.txt')
        return 1
    server_socket = None
    for port in range(settings.port, min(settings.port + 20, 65536)):
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            candidate.bind((settings.host, port))
            candidate.listen(128)
            server_socket = candidate
            break
        except OSError:
            candidate.close()
    if server_socket is None:
        print('附近端口均被占用，请在 .env 修改 HENGBAN_PORT 后重新运行。')
        return 1
    scheme='https' if settings.ssl_certfile and settings.ssl_keyfile else 'http'
    display_host=os.environ.get('SIMON_DISPLAY_HOST','127.0.0.1')
    url = f'{scheme}://{display_host}:{port}'
    print(f'Simon地址：{url}\n地图配置已加载：{bool(settings.amap_api_key)}\n保持此运行窗口开启；停止后网页将无法访问。', flush=True)
    if port != settings.port:
        print(f'端口 {settings.port} 被占用，已改用 {port}。', flush=True)

    def open_when_ready():
        for _ in range(40):
            try:
                with urlopen(url + '/api/health', timeout=1) as response:
                    body = json.load(response)
                if body.get('pid') == os.getpid():
                    if os.environ.get('HENGBAN_OPEN_BROWSER', '1') != '0':
                        webbrowser.open(url)
                    return
            except Exception:
                pass
            time.sleep(0.5)

    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        uvicorn.Server(uvicorn.Config(app, host=settings.host,port=port,workers=1,access_log=False,
            ssl_certfile=settings.ssl_certfile or None,ssl_keyfile=settings.ssl_keyfile or None)).run(sockets=[server_socket])
    finally:
        server_socket.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
