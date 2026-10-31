"""单用户手机配对：一次性短期配对码、设备独立凭据和分项数据范围。"""
import hashlib
import json
import secrets
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from fastapi import HTTPException
from app.personal.continuity import dump,stamp

SCOPES={'app','location','calendar','notifications','health','event_messages'}


class Devices:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:db.executescript('''
          CREATE TABLE IF NOT EXISTS simon_pairings(id TEXT PRIMARY KEY,code_hash TEXT UNIQUE,scopes TEXT,expires_at TEXT,used INTEGER);
          CREATE TABLE IF NOT EXISTS simon_devices(id TEXT PRIMARY KEY,name TEXT,platform TEXT,token_hash TEXT UNIQUE,scopes TEXT,permissions TEXT,revoked INTEGER,last_seen TEXT);
          CREATE TABLE IF NOT EXISTS simon_pair_attempts(address TEXT PRIMARY KEY,window_at TEXT,attempts INTEGER);
        ''')

    @staticmethod
    def digest(value):return hashlib.sha256(value.encode()).hexdigest()

    def pairing(self,scopes):
        if not scopes or not set(scopes)<=SCOPES:raise ValueError('请选择有效的设备数据范围。')
        code=secrets.token_hex(5).upper();expires=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat();key=uuid4().hex
        with self.store.connection() as db:db.execute('INSERT INTO simon_pairings VALUES (?,?,?,?,0)',(key,self.digest(code),dump(sorted(set(scopes))),expires))
        return {'id':key,'code':code,'expires_at':expires,'scopes':scopes,'note':'一次性配对码，5分钟有效；不代表手机操作系统已授予权限。'}

    def claim(self,code,name,platform,address):
        now=datetime.now(timezone.utc)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rate=db.execute('SELECT * FROM simon_pair_attempts WHERE address=?',(address,)).fetchone()
            count=rate['attempts'] if rate and now-datetime.fromisoformat(rate['window_at'])<timedelta(minutes=10) else 0
            if count>=10:raise HTTPException(429,'配对尝试过多，请10分钟后重试。')
            db.execute('INSERT INTO simon_pair_attempts VALUES (?,?,?) ON CONFLICT(address) DO UPDATE SET window_at=excluded.window_at,attempts=excluded.attempts',
                       (address,rate['window_at'] if count else now.isoformat(),count+1))
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            pairing=db.execute('SELECT * FROM simon_pairings WHERE code_hash=? AND used=0 AND expires_at>?',(self.digest(code.strip().upper()),now.isoformat())).fetchone()
            if not pairing:raise HTTPException(403,'配对码不存在、已使用或已过期。')
            token=secrets.token_urlsafe(32);key=uuid4().hex
            db.execute('INSERT INTO simon_devices VALUES (?,?,?,?,?,?,0,?)',(key,name,platform,self.digest(token),pairing['scopes'],'{}',stamp()))
            db.execute('UPDATE simon_pairings SET used=1 WHERE id=?',(pairing['id'],))
        return {'device_id':key,'token':token,'scopes':json.loads(pairing['scopes']),'name':name,'note':'凭据仅本次返回，保存在你的设备，不要放入URL或日志。'}

    def authenticate(self,authorization,required=None):
        token=authorization.removeprefix('Bearer ').strip() if authorization.startswith('Bearer ') else ''
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM simon_devices WHERE token_hash=? AND revoked=0',(self.digest(token),)).fetchone() if token else None
            if not row:raise HTTPException(401,'请先将此手机与Simon配对。')
            scopes=json.loads(row['scopes'])
            if required and required not in scopes:raise HTTPException(403,'此设备没有该项数据范围。')
            db.execute('UPDATE simon_devices SET last_seen=? WHERE id=?',(stamp(),row['id']))
        return {**{k:row[k] for k in ('id','name','platform','last_seen')},'scopes':scopes,'permissions':json.loads(row['permissions'])}

    def list(self):
        with self.store.connection() as db:rows=db.execute('SELECT id,name,platform,scopes,permissions,revoked,last_seen FROM simon_devices ORDER BY last_seen DESC').fetchall()
        return [{**dict(r),'scopes':json.loads(r['scopes']),'permissions':json.loads(r['permissions'])} for r in rows]

    def report(self,device,states):
        if not set(states)<=SCOPES:raise ValueError('权限类型不在白名单。')
        if not set(states)<=set(device['scopes']):raise HTTPException(403,'不能上报配对范围以外的权限。')
        value={**device['permissions'],**states}
        with self.store.connection() as db:db.execute('UPDATE simon_devices SET permissions=?,last_seen=? WHERE id=?',(dump(value),stamp(),device['id']))
        return {'states':value,'source':'paired_device_report','note':'记录接收端报告；是否真正授权仍由设备操作系统和原生采集端控制。'}

    def revoke(self,key):
        with self.store.connection() as db:
            if not db.execute('UPDATE simon_devices SET revoked=1 WHERE id=?',(key,)).rowcount:raise KeyError(key)
        return {'revoked':True,'device_id':key}

    @staticmethod
    def require_granted(device,scope):
        if scope not in device['scopes'] or device['permissions'].get(scope)!='granted':raise HTTPException(403,'请先在手机上授权该权限并上报授权状态。')
