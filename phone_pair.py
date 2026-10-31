"""在电脑终端生成一次性手机配对码；使用自己的开发CA验证本机HTTPS。"""
import argparse
import json
import ssl
from pathlib import Path
from urllib.request import Request,urlopen

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8443)
    parser.add_argument('--scopes',nargs='+',choices=['app','location','calendar','health','notifications','event_messages'],default=['app','location'])
    args=parser.parse_args()
    ca=Path(__file__).resolve().parent/'data'/'mobile-tls'/'ca.pem'
    context=ssl.create_default_context(cafile=str(ca))
    request=Request(f'https://127.0.0.1:{args.port}/api/mobile/pairings',data=json.dumps({'scopes':args.scopes}).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,context=context,timeout=10) as response:value=json.load(response)
    print('手机配对码：',value['code'],'\n5分钟有效，仅可使用一次。范围：',', '.join(value['scopes']))
