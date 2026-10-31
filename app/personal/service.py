"""单用户事件服务：持久状态、版本检查、授权提交、撤回和后台收件箱。"""
import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from uuid import uuid4
from app.config import settings
from app.personal.contracts import AutomationPolicy
from app.personal.planning import build_event_graph
from app.personal.ingestion import parse_calendar
from app.multi_agent.quantification import attach_quantification


def now_local():
    return datetime.now(timezone(timedelta(hours=8)))


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True)


class PersonalService:
    def __init__(self,service,start_worker=True):
        self.service=service
        self.store=service.store
        self.stop_event=threading.Event()
        self.last_calendar_check=None
        self.calendar_status='未连接；可在本机.env填写订阅地址并启用同步。'
        with self.store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS life_events (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(source,external_id));
                CREATE TABLE IF NOT EXISTS personal_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_actions (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
                    base_version INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(event_id,event_revision,base_version));
                CREATE TABLE IF NOT EXISTS health_reminders (
                    key TEXT PRIMARY KEY, due_at TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_outbox (
                    id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO personal_policy VALUES (1,1,?)',(encode(AutomationPolicy().model_dump()),))
        self.thread=threading.Thread(target=self._worker,daemon=True,name='hengban-personal-events')
        if start_worker:self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():self.thread.join(timeout=2)

    def policy(self):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM personal_policy WHERE id=1').fetchone()
        return {**json.loads(row['body']),'revision':row['revision']}

    def save_policy(self,policy):
        with self.service.lock,self.store.connection() as db:
            db.execute('UPDATE personal_policy SET body=?,revision=revision+1 WHERE id=1',(encode(policy),))
        self.sync_reminders()
        return self.policy()

    def events(self):
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM life_events ORDER BY updated_at DESC ').fetchall()
        return [{**json.loads(r['body']),'id':r['id'],'revision':r['revision']} for r in rows]

    def actions(self):
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM personal_actions ORDER BY created_at DESC,rowid DESC LIMIT 30').fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def outbox(self):
        with self.store.connection() as db:
            rows=db.execute("SELECT * FROM personal_outbox WHERE status!='superseded' ORDER BY created_at DESC,rowid DESC LIMIT 50").fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def notify(self,key,kind,body):
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO personal_outbox VALUES (?,?,?,?,?,?)',
                       (uuid4().hex,key,kind,'unread',encode(body),now_local().isoformat()))

    def acknowledge(self,item_id):
        with self.store.connection() as db:
            db.execute("UPDATE personal_outbox SET status='read' WHERE id=?",(item_id,))

    def snapshot(self):
        return {'events':self.events(),'actions':self.actions(),'outbox':self.outbox(),'policy':self.policy(),
                'calendar_configured':bool(settings.calendar_ics_url),'calendar_status':self.calendar_status,
                'last_calendar_check':self.last_calendar_check,'bridge_configured':bool(settings.event_bridge_token),
                'worker_alive':self.thread.is_alive()}

    def ingest(self,event,mode=None,process=True):
        with self.service.lock:
            event=dict(event)
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT * FROM life_events WHERE source=? AND external_id=?',(event['source'],event['external_id'])).fetchone()
                if old and event.get('source_sequence') is not None:
                    previous_sequence=json.loads(old['body']).get('source_sequence')
                    if previous_sequence is not None and event['source_sequence'] < previous_sequence:
                        return {'duplicate':True,'event_id':old['id'],'message':'忽略旧版本来源事件，未回退新日程。'}
                if old and json.loads(old['body']) == event:
                    return {'duplicate':True,'event_id':old['id'],'message':'相同事件版本已处理，未重复调整。'}
                event_id=old['id'] if old else uuid4().hex
                revision=old['revision']+1 if old else 1
                db.execute('INSERT INTO life_events VALUES (?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated_at=excluded.updated_at',
                           (event_id,event['source'],event['external_id'],revision,encode(event),now_local().isoformat()))
                db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
            if not process:return {'event_id':event_id,'message':'事件已保存。'}
            return self.process(event_id,mode)

    def process(self,event_id,mode=None):
        with self.service.lock:
            event=next(e for e in self.events() if e['id']==event_id)
            active=self.store.active_plan()
            if event['status']!='confirmed' or not active:
                return {'event_id':event_id,'message':'事件已保存；待定或取消事件不触发自动规划。' if active else '事件已保存；先确认健康计划后可检测冲突。'}
            if datetime.fromisoformat(event['end_at']) <= now_local():
                return {'event_id':event_id,'message':'历史事件已保存，未自动改写过去安排。'}
            profile=self.store.profile()
            if not profile or profile.get('needs_professional_support') is not False:
                return {'event_id':event_id,'message':'事件已保存；当前档案未进入一般日常规划范围。'}
            policy=self.policy()
            mode=mode or policy['agent_mode']
            action_id=uuid4().hex
            base_plan=attach_quantification(copy.deepcopy(active['body']),profile,self.store.records(),now_local().date().isoformat())
            body={'before_plan':active['body'],'business_revision':self.store.revision(),
                  'policy_revision':policy['revision'],'mode':mode,'event_title':event['title']}
            try:
                result=build_event_graph(self._runner(mode),self.service.memory).invoke({
                    'event':event,'events':self.events(),'base_plan':base_plan,'profile':profile,
                    'records':self.store.records(),'policy':policy,'now':now_local().isoformat(),
                    'role_results':[],'traces':[]})
                body.update({k:result[k] for k in ('candidate','changes','unresolved','impact','role_results','traces')})
                status='blocked' if result['unresolved'] else 'pending' if result['changes'] else 'observed'
            except Exception:
                status='failed';body['error']='事件已保存，但规划未完成；检查模型配置或使用离线演示，原计划未更改。'
            with self.store.connection() as db:
                db.execute('INSERT OR IGNORE INTO personal_actions VALUES (?,?,?,?,?,?,?)',
                           (action_id,event_id,event['revision'],active['version'],status,encode(body),now_local().isoformat()))
                stored=db.execute('SELECT id FROM personal_actions WHERE event_id=? AND event_revision=? AND base_version=?',
                                  (event_id,event['revision'],active['version'])).fetchone()
                action_id=stored['id']
            if status=='pending' and policy['auto_adjust']:
                action=self.decide(action_id,True)
                message='已按授权调整健康日程，时长不变；可以查看明细或撤回。'
            else:
                action=next(a for a in self.actions() if a['id']==action_id)
                message={'blocked':'存在需要你选择的冲突，未自动覆盖计划。','pending':'发现冲突并生成调整候选，请确认或启用自动调整。',
                         'observed':'事件已纳入时间线，当前没有可提交的活动时间调整。','failed':body.get('error','规划失败。')}[status]
            self.notify('event:'+action_id,'event',{'title':event['title'],'message':message,'action_id':action_id})
            return {'event_id':event_id,'action':action,'message':message}

    def _runner(self,mode):
        from app.multi_agent.runners import DeepSeekRunner,DemoRunner
        return DemoRunner() if mode=='demo' else DeepSeekRunner()

    def decide(self,action_id,approved):
        with self.service.lock,self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM personal_actions WHERE id=?',(action_id,)).fetchone()
            if not row:raise ValueError('调整不存在。')
            body=json.loads(row['body'])
            if row['status'] in ('applied','rejected'):
                if (row['status']=='applied') != approved:raise ValueError('已处理的决定不能改写，请使用撤回。')
                return {**dict(row),'body':body}
            if row['status']!='pending':raise ValueError('该调整不处于可确认状态。')
            status='rejected'
            if approved:
                current=db.execute('SELECT version FROM plans WHERE active=1').fetchone()
                event=db.execute('SELECT revision FROM life_events WHERE id=?',(row['event_id'],)).fetchone()
                revision=db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
                policy_revision=db.execute('SELECT revision FROM personal_policy WHERE id=1').fetchone()[0]
                if not current or current[0]!=row['base_version'] or not event or event[0]!=row['event_revision'] or revision!=body['business_revision'] or policy_revision!=body['policy_revision']:
                    raise ValueError('计划、事件、档案或授权已变化，请重新处理最新事件。')
                if any(datetime.fromisoformat(c['after'])<=now_local() for c in body['changes']):raise ValueError('候选时间已过去，请重新安排。')
                version=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM plans').fetchone()[0]
                db.execute('UPDATE plans SET active=0 WHERE active=1')
                db.execute('INSERT INTO plans VALUES (?,?,?,?,?)',(version,'personal:'+action_id,encode(body['candidate']),1,now_local().isoformat()))
                db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
                body.update(applied_version=version,applied_revision=revision+1)
                status='applied'
            db.execute('UPDATE personal_actions SET status=?,body=? WHERE id=?',(status,encode(body),action_id))
        self.sync_reminders()
        return next(a for a in self.actions() if a['id']==action_id)

    def undo(self,action_id):
        with self.service.lock,self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM personal_actions WHERE id=?',(action_id,)).fetchone()
            if not row or row['status']!='applied':raise ValueError('只有已应用的最新调整可以撤回。')
            body=json.loads(row['body'])
            current=db.execute('SELECT version FROM plans WHERE active=1').fetchone()
            revision=db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
            if not current or current[0]!=body['applied_version'] or revision!=body['applied_revision']:
                raise ValueError('后续已有其他修改，不能直接覆盖；请重新规划。')
            version=db.execute('SELECT MAX(version)+1 FROM plans').fetchone()[0]
            db.execute('UPDATE plans SET active=0 WHERE active=1')
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?)',(version,'undo:'+action_id,encode(body['before_plan']),1,now_local().isoformat()))
            db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
            db.execute("UPDATE personal_actions SET status='undone' WHERE id=?",(action_id,))
        self.sync_reminders()
        return {'message':'已恢复调整前版本并保留历史。原事件仍存在，请注意可能重新出现的时间冲突。'}

    def sync_reminders(self):
        with self.service.lock:
            active=self.store.active_plan();policy=self.policy();now=now_local()
            desired={}
            if active and policy['reminders_enabled']:
                for day in active['body'].get('daily_schedule',[]):
                    if day.get('kind')!='exercise':continue
                    start=datetime.fromisoformat(day['start_at'])
                    due=start-timedelta(minutes=policy['reminder_before_minutes'])
                    if start<=now:continue
                    key=f"plan:{active['version']}:{day['start_at']}"
                    desired[key]=(due.isoformat(),{'title':day['title'],'start_at':day['start_at'],'minutes':day['minutes'],'energy':day.get('energy'),
                                                   'message':f"计划{day['minutes']}分钟；请确认天气与当前状态。",'plan_version':active['version']})
            with self.store.connection() as db:
                for item in db.execute("SELECT id,body FROM personal_outbox WHERE kind='reminder' AND status='unread'").fetchall():
                    item_body=json.loads(item['body']);version=item_body.get('plan_version')
                    if not policy['reminders_enabled'] or version is not None and (not active or version!=active['version']):
                        db.execute("UPDATE personal_outbox SET status='superseded' WHERE id=?",(item['id'],))
                for row in db.execute("SELECT key FROM health_reminders WHERE status='pending'").fetchall():
                    if row['key'].startswith('plan:') and row['key'] not in desired:db.execute("UPDATE health_reminders SET status='cancelled' WHERE key=?",(row['key'],))
                for key,(due,body) in desired.items():
                    db.execute("INSERT INTO health_reminders VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET due_at=excluded.due_at,body=excluded.body,status=CASE WHEN health_reminders.status='cancelled' THEN 'pending' ELSE health_reminders.status END",
                               (key,due,'pending',encode(body)))

    def tick(self):
        from zoneinfo import ZoneInfo
        self.sync_reminders();policy=self.policy();now=datetime.now(ZoneInfo(policy.get('timezone_name','Asia/Shanghai')))
        clock=now.strftime('%H:%M');a,b=policy['quiet_start'],policy['quiet_end']
        quiet=a<=clock<b if a<b else clock>=a or clock<b if a!=b else False
        with self.store.connection() as db:
            for row in db.execute("SELECT * FROM health_reminders WHERE status='pending'").fetchall():
                body=json.loads(row['body'])
                if datetime.fromisoformat(row['due_at'])>now:continue
                if datetime.fromisoformat(body.get('expires_at',body['start_at']))<=now:
                    db.execute("UPDATE health_reminders SET status='expired' WHERE key=?",(row['key'],));continue
                if quiet:continue
                # 提醒入收件箱与状态更新在同一事务中，不把入队当作用户已看到。
                db.execute('INSERT OR IGNORE INTO personal_outbox VALUES (?,?,?,?,?,?)',
                           (uuid4().hex,row['key'],'reminder','unread',row['body'],now.isoformat()))
                db.execute("UPDATE health_reminders SET status='queued' WHERE key=?",(row['key'],))

    def import_calendar(self,content):
        events,warnings=parse_calendar(content)
        saved=[self.ingest(e,process=False) for e in events]
        results=[self.process(item['event_id']) if not item.get('duplicate') else item for item in saved]
        return {'imported':len(events),'results':results,'warnings':warnings,
                'message':'按UID更新日历事件；从订阅中消失不等于取消，请源日历提供CANCELLED更新。'}

    def sync_calendar(self):
        if not settings.calendar_ics_url:raise ValueError('请在本机.env填写HENGBAN_CALENDAR_ICS_URL后重启。')
        if urlparse(settings.calendar_ics_url).scheme!='https':raise ValueError('日历订阅只接受HTTPS地址。')
        try:
            with urlopen(Request(settings.calendar_ics_url,headers={'User-Agent':'Hengban-calendar/1'}),timeout=10) as response:
                content=response.read(1_000_001)
            if len(content)>1_000_000:raise ValueError('日历过大，请缩小导出范围。')
            result=self.import_calendar(content.decode('utf-8-sig'))
            self.calendar_status=f"最近同步导入{result['imported']}个单次事件；提示{len(result['warnings'])}项。"
            return result
        except Exception:
            self.calendar_status='日历同步失败，未据此取消现有事件；检查订阅配置或改用ICS导入。'
            raise ValueError(self.calendar_status) from None
        finally:self.last_calendar_check=now_local().isoformat()

    def _worker(self):
        count=0
        while not self.stop_event.wait(5):
            if not self.service.lock.acquire(blocking=False):continue
            try:
                self.tick();count+=1
                if count%12==0 and self.policy()['calendar_enabled'] and settings.calendar_ics_url:
                    self.sync_calendar()
            except Exception:
                # 异常只记录受控状态；不输出URL、消息正文和密钥。
                self.calendar_status='后台检查暂未完成，保留已有记录。'
            finally:self.service.lock.release()
