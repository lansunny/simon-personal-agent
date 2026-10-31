"""持久通知投递：领取与回执分离，结果不明不盲目重发。"""
import json
from datetime import datetime,timedelta,timezone
from urllib.request import Request,urlopen
from urllib.parse import urlparse
from uuid import uuid4
from app.config import settings
from app.personal.continuity import dump,stamp


class Delivery:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS personal_deliveries (
                outbox_id TEXT PRIMARY KEY,status TEXT NOT NULL,claim_token TEXT,lease_until TEXT,
                channel TEXT NOT NULL,attempts INTEGER NOT NULL,receipt TEXT,updated_at TEXT NOT NULL);''')

    def list(self):
        with self.store.connection() as db:rows=db.execute('SELECT * FROM personal_deliveries ORDER BY updated_at DESC LIMIT 100').fetchall()
        return [{k:(json.loads(v) if k=='receipt' and v else v) for k,v in dict(r).items() if k!='claim_token'} for r in rows]

    def claim(self,channel='bridge',limit=10):
        now=datetime.now(timezone.utc);result=[]
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE personal_deliveries SET status='unknown',updated_at=? WHERE status='claimed' AND lease_until<?",(stamp(),now.isoformat()))
            rows=db.execute("SELECT o.* FROM personal_outbox o LEFT JOIN personal_deliveries d ON d.outbox_id=o.id WHERE o.status='unread' AND (d.outbox_id IS NULL OR d.status='pending') ORDER BY o.created_at LIMIT ?",(min(limit,20),)).fetchall()
            for row in rows:
                body=json.loads(row['body'])
                if body.get('expires_at') and datetime.fromisoformat(body['expires_at'])<=now:
                    db.execute("UPDATE personal_outbox SET status='superseded' WHERE id=?",(row['id'],));continue
                token=uuid4().hex
                db.execute("INSERT INTO personal_deliveries VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(outbox_id) DO UPDATE SET status='claimed',claim_token=excluded.claim_token,lease_until=excluded.lease_until,attempts=personal_deliveries.attempts+1,channel=excluded.channel,updated_at=excluded.updated_at",
                           (row['id'],'claimed',token,(now+timedelta(seconds=90)).isoformat(),channel,1,None,stamp()))
                result.append({'id':row['id'],'claim_token':token,'body':json.loads(row['body']),'kind':row['kind']})
        return result

    def receipt(self,outbox_id,claim_token,status,provider_id=''):
        if status not in ('accepted','displayed','failed','unknown'):raise ValueError('投递回执状态无效。')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM personal_deliveries WHERE outbox_id=?',(outbox_id,)).fetchone()
            if not row or row['claim_token']!=claim_token:raise ValueError('投递领取标识无效。')
            if row['status']!='claimed' and not (row['status'] in ('accepted','unknown') and status in ('accepted','displayed')):
                if row['status']==status:return {'status':status,'duplicate':True}
                raise ValueError('该次投递已有回执，不得覆盖。')
            db.execute('UPDATE personal_deliveries SET status=?,receipt=?,updated_at=? WHERE outbox_id=?',
                       (status,dump({'provider_id':provider_id,'status':status}),stamp(),outbox_id))
        return {'status':status,'note':'accepted仅表示接收端接受；displayed由已认证接收端报告，不表示用户完成事项。'}

    def retry(self,outbox_id):
        with self.store.connection() as db:
            row=db.execute('SELECT status FROM personal_deliveries WHERE outbox_id=?',(outbox_id,)).fetchone()
            if not row:raise KeyError(outbox_id)
            if row['status'] not in ('failed','unknown'):raise ValueError('仅失败或结果不明的投递可重试。')
            db.execute("UPDATE personal_deliveries SET status='pending',claim_token=NULL,lease_until=NULL,updated_at=? WHERE outbox_id=?",(stamp(),outbox_id))
        return {'status':'pending','note':'用户明确重试，接收端仍应按outbox_id幂等。'}

    def webhook(self):
        if not settings.notification_webhook_url:return
        if urlparse(settings.notification_webhook_url).scheme!='https':return
        for item in self.claim('webhook',3):
            payload={'id':item['id'],'kind':item['kind'],'notification':item['body'],'claim_token':item['claim_token']}
            headers={'Content-Type':'application/json','Idempotency-Key':item['id']}
            if settings.notification_webhook_token:headers['Authorization']='Bearer '+settings.notification_webhook_token
            try:
                request=Request(settings.notification_webhook_url,data=dump(payload).encode(),headers=headers,method='POST')
                with urlopen(request,timeout=8) as response:response.read(1024)
                status='accepted'
            except Exception:status='unknown'
            self.receipt(item['id'],item['claim_token'],status)
