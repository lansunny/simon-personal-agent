"""SQLite任务队列：短事务领取、进程外持久、租约恢复和有界重试。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from app.personal.continuity import dump,stamp
from app.errors import BudgetExceeded


def utcnow():return datetime.now(timezone.utc)


def next_fire(schedule,after):
    zone=ZoneInfo(schedule.get('timezone_name','Asia/Shanghai'))
    after=after.astimezone(zone)
    if schedule['frequency']=='interval':return (after+timedelta(minutes=schedule['interval_minutes'])).astimezone(timezone.utc)
    hour,minute=map(int,schedule['time'].split(':'))
    for n in range(8):
        value=(after+timedelta(days=n)).replace(hour=hour,minute=minute,second=0,microsecond=0)
        if value>after and (schedule['frequency']=='daily' or value.weekday() in schedule['weekdays']):return value.astimezone(timezone.utc)
    raise ValueError('无法计算下次执行时间。')


class JobRuntime:
    def __init__(self,store,handler,maintenance=None,start_worker=True):
        self.store,self.handler,self.maintenance=store,handler,maintenance
        self.owner=uuid4().hex;self.stopping=threading.Event();self.future=None;self.active_job=None
        self.dispatch_lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='health-job')
        self.maintenance_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='health-reminders')
        self.maintenance_future=None
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS personal_jobs (
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,dedupe_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,run_at TEXT NOT NULL,attempts INTEGER NOT NULL,max_attempts INTEGER NOT NULL,
                    owner TEXT,lease_until TEXT,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_jobs_due ON personal_jobs(status,run_at);
                CREATE TABLE IF NOT EXISTS personal_job_attempts (
                    job_id TEXT NOT NULL,attempt INTEGER NOT NULL,status TEXT NOT NULL,thread_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,finished_at TEXT,error TEXT,PRIMARY KEY(job_id,attempt));
                CREATE TABLE IF NOT EXISTS personal_schedules (
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,enabled INTEGER NOT NULL,body TEXT NOT NULL,next_run_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_usage (
                    day TEXT NOT NULL,kind TEXT NOT NULL,used INTEGER NOT NULL,PRIMARY KEY(day,kind));
            ''')
        self.thread=threading.Thread(target=self._loop,daemon=True,name='health-scheduler')
        if start_worker:self.thread.start()

    def enqueue(self,kind,payload,dedupe_key,run_at=None,max_attempts=3):
        when=(run_at or utcnow()).astimezone(timezone.utc).isoformat()
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO personal_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (uuid4().hex,kind,'pending',dedupe_key,dump(payload),when,0,max_attempts,None,None,None,None,stamp(),stamp()))
            row=db.execute('SELECT * FROM personal_jobs WHERE dedupe_key=?',(dedupe_key,)).fetchone()
            if row['kind']!=kind or json.loads(row['payload'])!=payload:raise ValueError('任务幂等标识已用于不同内容。')
        return self.get(row['id'])

    def get(self,job_id):
        with self.store.connection() as db:row=db.execute('SELECT * FROM personal_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise KeyError(job_id)
        result=dict(row);result['payload']=json.loads(result['payload']);result['result']=json.loads(result['result']) if result['result'] else None
        result.pop('owner',None)
        return result

    def list(self,limit=50):
        with self.store.connection() as db:ids=[r[0] for r in db.execute('SELECT id FROM personal_jobs ORDER BY created_at DESC,rowid DESC LIMIT ?',(min(limit,200),))]
        return [self.get(i) for i in ids]

    def consume(self,kind,limit,timezone_name='Asia/Shanghai',amount=1):
        day=utcnow().astimezone(ZoneInfo(timezone_name)).date().isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT used FROM personal_usage WHERE day=? AND kind=?',(day,kind)).fetchone()
            used=row['used'] if row else 0
            if used+amount>limit:raise BudgetExceeded(kind,used,limit,day,timezone_name)
            db.execute('INSERT INTO personal_usage VALUES (?,?,?) ON CONFLICT(day,kind) DO UPDATE SET used=excluded.used',(day,kind,used+amount))
        return {'used':used+amount,'limit':limit,'day':day}

    def usage(self):
        with self.store.connection() as db:return [dict(r) for r in db.execute('SELECT * FROM personal_usage ORDER BY day DESC LIMIT 30')]

    def budgets(self,policy):
        zone=ZoneInfo(policy['timezone_name']);day=utcnow().astimezone(zone).date().isoformat()
        reset_at=(datetime.fromisoformat(day)+timedelta(days=1)).replace(tzinfo=zone).isoformat()
        with self.store.connection() as db:usage={row['kind']:row['used'] for row in db.execute('SELECT * FROM personal_usage WHERE day=?',(day,))}
        return {kind:{'used':usage.get(kind,0),'limit':policy[field],'remaining':max(0,policy[field]-usage.get(kind,0)),
                      'day':day,'reset_at':reset_at} for kind,field in (('model','model_daily_budget'),('external','external_daily_budget'))}

    def add_schedule(self,body,schedule_id=None):
        key=schedule_id or uuid4().hex
        value=next_fire(body,utcnow())
        with self.store.connection() as db:
            db.execute('INSERT INTO personal_schedules VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,enabled=excluded.enabled,body=excluded.body,next_run_at=excluded.next_run_at',
                       (key,body['title'],int(body['enabled']),dump(body),value.isoformat()))
        return next(s for s in self.schedules() if s['id']==key)

    def schedules(self):
        with self.store.connection() as db:rows=db.execute('SELECT * FROM personal_schedules ORDER BY rowid DESC').fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def schedule_enabled(self,schedule_id,enabled):
        with self.store.connection() as db:db.execute('UPDATE personal_schedules SET enabled=? WHERE id=?',(int(enabled),schedule_id))

    def cancel(self,job_id):
        with self.store.connection() as db:
            row=db.execute('SELECT status FROM personal_jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise KeyError(job_id)
            if row['status']=='running':raise ValueError('任务正在执行，不能假定已取消；可关闭后续自动执行授权。')
            db.execute("UPDATE personal_jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('pending','retry','failed')",(stamp(),job_id))
        return self.get(job_id)

    def retry(self,job_id):
        with self.store.connection() as db:
            db.execute("UPDATE personal_jobs SET status='retry',max_attempts=attempts+1,run_at=?,error=NULL,updated_at=? WHERE id=? AND status='failed'",(stamp(),stamp(),job_id))
        return self.get(job_id)

    def _admit_schedules(self):
        now=utcnow()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT * FROM personal_schedules WHERE enabled=1 AND next_run_at<=?',(now.isoformat(),)).fetchall()
            for row in rows:
                body=json.loads(row['body']);payload={'goal_id':body.get('goal_id'),'schedule_id':row['id'],'scheduled_at':row['next_run_at']}
                key=f"schedule:{row['id']}:{row['next_run_at']}"
                db.execute('INSERT OR IGNORE INTO personal_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (uuid4().hex,body['kind'],'pending',key,dump(payload),now.isoformat(),0,3,None,None,None,None,stamp(),stamp()))
                # 错过多轮只产生一个当前检查，不补发所有旧提醒。
                db.execute('UPDATE personal_schedules SET next_run_at=? WHERE id=?',(next_fire(body,now).isoformat(),row['id']))

    def _claim(self,job_id=None):
        now=utcnow();until=(now+timedelta(seconds=45)).isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE personal_job_attempts SET status='interrupted',finished_at=?,error='执行租约失效' WHERE status='running' AND job_id IN (SELECT id FROM personal_jobs WHERE status='running' AND lease_until<?)",(now.isoformat(),now.isoformat()))
            db.execute("UPDATE personal_jobs SET status=CASE WHEN attempts<max_attempts THEN 'retry' ELSE 'failed' END,owner=NULL,lease_until=NULL,error='执行租约已失效，按幂等任务重试',updated_at=? WHERE status='running' AND lease_until<?",(now.isoformat(),now.isoformat()))
            sql="SELECT * FROM personal_jobs WHERE status IN ('pending','retry') AND run_at<=? AND attempts<max_attempts";args=[now.isoformat()]
            if job_id:sql+=' AND id=?';args.append(job_id)
            row=db.execute(sql+' ORDER BY run_at,rowid LIMIT 1',args).fetchone()
            if not row:return None
            db.execute("UPDATE personal_jobs SET status='running',attempts=attempts+1,owner=?,lease_until=?,updated_at=? WHERE id=?",(self.owner,until,stamp(),row['id']))
            attempt=row['attempts']+1
            db.execute('INSERT INTO personal_job_attempts VALUES (?,?,?,?,?,?,?)',(row['id'],attempt,'running','personal-job:'+row['id'],stamp(),None,None))
        return self.get(row['id'])

    def _execute(self,job):
        error=None;result=None
        try:
            result=self.handler(job)
            status='succeeded'
        except BudgetExceeded as exc:
            error=str(exc);status='failed'
        except Exception as exc:
            # 不把URL、模型响应正文或外部消息写入错误日志。
            error=f'{type(exc).__name__}: 后台任务未完成，请检查输入、授权或配置。'
            status='retry' if job['attempts']<job['max_attempts'] else 'failed'
        with self.store.connection() as db:
            db.execute("UPDATE personal_jobs SET status=?,result=?,error=?,owner=NULL,lease_until=NULL,run_at=?,updated_at=? WHERE id=? AND owner=? AND status='running'",
                       (status,dump(result) if result is not None else None,error,(utcnow()+timedelta(seconds=min(300,5*2**job['attempts']))).isoformat(),stamp(),job['id'],self.owner))
            db.execute('UPDATE personal_job_attempts SET status=?,finished_at=?,error=? WHERE job_id=? AND attempt=?',(status,stamp(),error,job['id'],job['attempts']))
        return self.get(job['id'])

    def run_now(self,job_id):
        # 手动执行与后台领取仍走同一状态机，不能重复运行已完成或正在运行的任务。
        with self.dispatch_lock:
            if self.active_job is not None:raise ValueError('该用户已有后台任务执行中。')
            with self.store.connection() as db:db.execute("UPDATE personal_jobs SET run_at=? WHERE id=? AND status IN ('pending','retry')",(stamp(),job_id))
            job=self._claim(job_id)
            if not job:return self.get(job_id)
            self.active_job=job['id']
        try:return self._execute(job)
        finally:
            with self.dispatch_lock:self.active_job=None

    def tick(self):
        self._admit_schedules()
        # 提醒与投递有独立线程，不被模型任务阻塞，也不阻塞执行租约续期。
        if self.maintenance and (self.maintenance_future is None or self.maintenance_future.done()):
            self.maintenance_future=self.maintenance_pool.submit(self.maintenance)
        if self.active_job is not None and (self.future is None or not self.future.done()):
            with self.store.connection() as db:db.execute("UPDATE personal_jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'",((utcnow()+timedelta(seconds=45)).isoformat(),self.active_job,self.owner))
            return
        with self.dispatch_lock:
            if self.active_job is not None:
                if self.future is not None and self.future.done():self.active_job=None;self.future=None
                else:return
            job=self._claim()
            if job:self.active_job=job['id'];self.future=self.pool.submit(self._execute,job)

    def _loop(self):
        while not self.stopping.wait(2):
            try:self.tick()
            except Exception:pass

    def close(self):
        self.stopping.set()
        if self.thread.is_alive():self.thread.join(timeout=3)
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.maintenance_pool.shutdown(wait=True,cancel_futures=True)
