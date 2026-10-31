"""安卓局域网开发入口：准备独立开发CA，不安装信任、不修改.env或防火墙。"""
import argparse
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import subprocess

ROOT=Path(__file__).resolve().parent


def prepare(ip):
    ipaddress.ip_address(ip)
    executable=shutil.which('openssl')
    if not executable:raise RuntimeError('需要OpenSSL。也可自行配置SIMON_SSL_CERTFILE、SIMON_SSL_KEYFILE后运行run.py。')
    directory=ROOT/'data'/'mobile-tls';directory.mkdir(parents=True,exist_ok=True)
    request_config=directory/'request.cnf'
    request_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=Simon Local Development\n',encoding='ascii')
    def command(*args):
        if args[0]=='req':args=(*args,'-config',request_config)
        subprocess.run([executable,*map(str,args)],check=True,capture_output=True)
    ca=directory/'ca.pem';key=directory/'ca.key'
    if not ca.exists():
        command('req','-x509','-newkey','rsa:2048','-sha256','-nodes','-keyout',key,'-out',ca,'-days','730','-subj','/CN=Simon Local Development CA',
                '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    command('x509','-in',ca,'-outform','DER','-out',directory/'Simon-dev-CA.cer')
    server=directory/'server.pem';server_key=directory/'server.key'
    marker=directory/'server-ip.txt'
    valid=server.exists() and marker.exists() and marker.read_text().strip()==ip
    if valid:
        valid=subprocess.run([executable,'x509','-in',str(server),'-checkend','86400','-noout'],capture_output=True).returncode==0
    if not valid:
        config=directory/'extensions.cnf'
        config.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:'+ip+',IP:127.0.0.1,DNS:localhost\n',encoding='ascii')
        command('req','-new','-newkey','rsa:2048','-nodes','-keyout',server_key,'-out',directory/'server.csr','-subj','/CN=Simon Local Server')
        command('x509','-req','-in',directory/'server.csr','-CA',ca,'-CAkey',key,'-CAcreateserial','-out',server,'-days','90','-sha256','-extfile',config)
        marker.write_text(ip,encoding='ascii')
    return directory,server,server_key


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip',help='电脑与手机同一网络的IPv4地址')
    parser.add_argument('--port',type=int,default=8443)
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    ip=args.ip
    if not ip:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
            probe.connect(('8.8.8.8',80));ip=probe.getsockname()[0]
    directory,cert,key=prepare(ip)
    print(f'Simon安卓服务地址：https://{ip}:{args.port}',flush=True)
    print(f'安卓debug包需由你安装开发CA：{directory / "Simon-dev-CA.cer"}\n仅传输.cer，ca.key/server.key保留电脑，不能发送到手机。',flush=True)
    if args.prepare_only:return
    # 由用户运行这个入口才绑定局域网；默认run.py仍仅监听本机。
    os.environ.update(SIMON_HOST='0.0.0.0',SIMON_SSL_CERTFILE=str(cert),SIMON_SSL_KEYFILE=str(key),SIMON_DISPLAY_HOST=ip,HENGBAN_PORT=str(args.port),HENGBAN_OPEN_BROWSER='0')
    from run import main as serve
    return serve()


if __name__=='__main__':raise SystemExit(main())
