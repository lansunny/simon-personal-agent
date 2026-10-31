"""跨 run 的个人目标、事项、工作状态和可追溯反馈。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.multi_agent.quantification import nutrition_total


def stamp():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True)


class Continuity:
    def __init__(self,store,memory):
        self.store,self.memory=store,memory
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS personal_objects (
                    kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
                    status TEXT NOT NULL,body TEXT NOT NULL,updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS personal_session_messages (
                    session_id TEXT NOT NULL,message_id TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id,message_id));
                CREATE TABLE IF NOT EXISTS personal_journal (
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,entity_id TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_journal_entity ON personal_journal(kind,entity_id,created_at);
                CREATE TABLE IF NOT EXISTS personal_record_projections (
                    day TEXT NOT NULL,field TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(day,field));
            ''')

    def get(self,kind,object_id):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM personal_objects WHERE kind=? AND id=?',(kind,object_id)).fetchone()
        if not row:raise KeyError(object_id)
        return {**json.loads(row['body']),'id':row['id'],'revision':row['revision'],'status':row['status'],'updated_at':row['updated_at']}

    def list(self,kind,status=None,limit=100):
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM personal_objects WHERE kind=? ORDER BY updated_at DESC,rowid DESC',(kind,)).fetchall()
        values=[{**json.loads(r['body']),'id':r['id'],'revision':r['revision'],'status':r['status'],'updated_at':r['updated_at']} for r in rows]
        return [v for v in values if status is None or v['status']==status][:limit]

    def put(self,kind,object_id,body,status='active',expected_revision=None,db=None):
        if db is None:
            with self.store.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                self.put(kind,object_id,body,status,expected_revision,connection)
            return self.get(kind,object_id)
        old=db.execute('SELECT revision FROM personal_objects WHERE kind=? AND id=?',(kind,object_id)).fetchone()
        if expected_revision is not None and (old['revision'] if old else 0)!=expected_revision:
            raise ValueError('事项已被其他请求修改，请重新读取。')
        cleaned={k:v for k,v in body.items() if k not in ('id','revision','status','updated_at')}
        db.execute('INSERT INTO personal_objects VALUES (?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=personal_objects.revision+1,status=excluded.status,body=excluded.body,updated_at=excluded.updated_at',
                   (kind,object_id,1,status,dump(cleaned),stamp()))

    def append(self,kind,entity_id,body,dedupe_key,db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.append(kind,entity_id,body,dedupe_key,connection)
        existing=db.execute('SELECT id,body FROM personal_journal WHERE dedupe_key=?',(dedupe_key,)).fetchone()
        if existing:
            if json.loads(existing['body'])!=body:raise ValueError('幂等标识已经用于不同内容。')
            return existing['id']
        item_id=uuid4().hex
        db.execute('INSERT INTO personal_journal VALUES (?,?,?,?,?,?)',(item_id,kind,entity_id,dedupe_key,dump(body),stamp()))
        return item_id

    def journal(self,kind,entity_id=None,limit=30):
        with self.store.connection() as db:
            sql='SELECT * FROM personal_journal WHERE kind=?';args=[kind]
            if entity_id:sql+=' AND entity_id=?';args.append(entity_id)
            rows=db.execute(sql+' ORDER BY created_at DESC,rowid DESC LIMIT ?',(*args,min(limit,100))).fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def session(self,title='健康日常',timezone_name='Asia/Shanghai',session_id=None):
        if session_id:return self.get('session',session_id)
        return self.put('session',uuid4().hex,{'title':title,'timezone_name':timezone_name,'run_ids':[],'goal_ids':[]})

    def message(self,session_id,role,content,run_id=None,dedupe_key=None):
        self.get('session',session_id)
        message_id=self.memory.message(role,content,run_id,dedupe_key)
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO personal_session_messages VALUES (?,?,?)',(session_id,message_id,stamp()))
        return message_id

    def messages(self,session_id,limit=20):
        self.get('session',session_id)
        with self.store.connection() as db:
            rows=db.execute('''SELECT m.* FROM personal_session_messages link JOIN messages m ON m.id=link.message_id
                WHERE link.session_id=? ORDER BY link.created_at DESC,link.rowid DESC LIMIT ?''',(session_id,min(limit,100))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def link_run(self,session_id,run_id,goal_id=None):
        session=self.get('session',session_id)
        session['run_ids']=list(dict.fromkeys(session.get('run_ids',[])+[run_id]))[-100:]
        if goal_id:session['goal_ids']=list(dict.fromkeys(session.get('goal_ids',[])+[goal_id]))
        self.put('session',session_id,session,expected_revision=session['revision'])
        self.put('run_link',run_id,{'session_id':session_id,'goal_id':goal_id})

    def create_goal(self,body):
        if body.get('session_id'):self.get('session',body['session_id'])
        goal=self.put('goal',uuid4().hex,{**body,'created_at':stamp()})
        if body.get('session_id'):
            session=self.get('session',body['session_id'])
            session['goal_ids']=list(dict.fromkeys(session.get('goal_ids',[])+[goal['id']]))
            self.put('session',session['id'],session,expected_revision=session['revision'])
        return goal

    def create_task(self,body):
        if body.get('goal_id'):self.get('goal',body['goal_id'])
        if body.get('session_id'):self.get('session',body['session_id'])
        return self.put('task',uuid4().hex,{**body,'origin':'user','created_at':stamp()},'scheduled' if body.get('start_at') else 'waiting_information')

    def sync_plan(self,plan,goal_id=None):
        if not plan:return []
        body=plan['body'];items=[]
        for entry in body.get('daily_schedule',[]):
            if entry.get('kind')=='exercise':
                items.append({'logical_id':entry.get('logical_id',f"activity:{entry.get('origin_date',entry['date'])}"),
                              'kind':'activity','title':entry['title'],'start_at':entry['start_at'],'end_at':entry['end_at'],
                              'quantities':{'minutes':entry['minutes'],'energy':entry.get('energy'),'distance_m':entry.get('round_trip_distance_m')},'date':entry['date']})
        for entry in body.get('meal_schedule',[]):
            items.append({'logical_id':entry.get('logical_id',f"meal:{entry['date']}:{entry['meal']}"),'kind':'meal',
                          'title':entry.get('title','用餐安排'),'start_at':entry['start_at'],'end_at':entry['end_at'],
                          'quantities':entry.get('nutrition',{}),'date':entry['date'],'assumed':entry.get('assumed',False)})
        for entry in body.get('sleep_schedule',[]):
            if entry.get('start_at') and entry.get('end_at'):
                items.append({'logical_id':entry.get('logical_id',f"sleep:{entry['date']}"),'kind':'sleep','title':'计划休息窗口',
                              'start_at':entry['start_at'],'end_at':entry['end_at'],'date':entry['date'],
                              'quantities':{'opportunity_hours':entry['opportunity_hours'],'target_hours':entry['target_hours']}})
        keys=set()
        for item in items:
            object_id='plan-task-'+hashlib.sha256(item['logical_id'].encode()).hexdigest()[:24];keys.add(object_id)
            try:old=self.get('task',object_id)
            except KeyError:old={}
            if old.get('status') in ('completed','partial','skipped'):continue
            linked_goal=goal_id
            if linked_goal:
                domain={'meal':'diet','activity':'activity','sleep':'sleep'}[item['kind']]
                if domain not in self.get('goal',linked_goal)['domains']:linked_goal=None
            new={**old,**item,'goal_id':linked_goal or old.get('goal_id'),'origin':'plan','plan_version':plan['version']}
            if all(old.get(k)==v for k,v in new.items() if k not in ('revision','updated_at','status')):continue
            self.put('task',object_id,new,'waiting_confirmation' if item.get('assumed') else 'scheduled')
        for old in self.list('task',limit=10000):
            if old.get('origin')=='plan' and old['id'] not in keys and old['status'] in ('scheduled','waiting_information'):
                self.put('task',old['id'],old,'cancelled',old['revision'])
        return [self.get('task',key) for key in keys]

    def feedback(self,task_id,body):
        task=self.get('task',task_id)
        if task['status']=='cancelled':raise ValueError('事项已取消，请选择有效事项。')
        key='feedback:'+body['idempotency_key']
        with self.store.connection() as db:
            existing=db.execute('SELECT * FROM personal_journal WHERE dedupe_key=?',(key,)).fetchone()
            if existing:
                if json.loads(existing['body'])!={'task_id':task_id,**body}:raise ValueError('反馈幂等标识已用于不同内容。')
                return {'feedback_id':existing['id'],'duplicate':True,'task':self.get('task',task_id)}
        source=self.memory.message('user',body.get('note') or f"事项{task_id}反馈：{body['outcome']}",dedupe_key=key+':source')
        # 该来源与反馈事务分别持久，但幂等键确保反馈不会被重复计入。
        payload={'task_id':task_id,**body}
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            feedback_id=self.append('feedback',task_id,payload,key,db)
            row=db.execute("SELECT revision FROM personal_objects WHERE kind='task' AND id=?",(task_id,)).fetchone()
            from zoneinfo import ZoneInfo
            actual=datetime.fromisoformat(body['recorded_at']) if body.get('recorded_at') else datetime.now(timezone.utc)
            if actual>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('不能记录未来的执行反馈。')
            zone=ZoneInfo((self.store.profile() or {}).get('timezone_name','Asia/Shanghai'))
            task.update(last_feedback=body,feedback_source_id=source,actual_date=actual.astimezone(zone).date().isoformat())
            if body.get('meal'):task['observed_nutrition']=nutrition_total(body['meal']['items'])
            self.put('task',task_id,task,body['outcome'],row['revision'],db)
        self.project_records(task['actual_date'])
        return {'feedback_id':feedback_id,'duplicate':False,'task':self.get('task',task_id),'source_message_id':source}

    def project_records(self,day):
        tasks=[t for t in self.list('task',limit=10000) if (t.get('actual_date') or t.get('date') or str(t.get('start_at',''))[:10])==day and t.get('last_feedback')]
        minutes=[t['last_feedback']['actual_minutes'] for t in tasks if t['kind']=='activity' and t['last_feedback'].get('actual_minutes') is not None]
        sleeps=[t['last_feedback']['sleep_hours'] for t in tasks if t['kind']=='sleep' and t['last_feedback'].get('sleep_hours') is not None]
        values={}
        # 修正反馈撤回数值时，清除本系统投影的旧值，不能继续展示失效实际值。
        for field,observations in [('activity_minutes',minutes),('sleep_hours',sleeps)]:
            if not observations:
                with self.store.connection() as db:
                    if db.execute('SELECT 1 FROM personal_record_projections WHERE day=? AND field=?',(day,field)).fetchone():values[field]=None
        if minutes:
            if sum(minutes)>1440:
                self.put('observation','activity-conflict:'+day,{'kind':'data_conflict','date':day,'note':'同日活动反馈合计超过1440分钟，未投影为正式日记录，请核对。'},'pending_review')
            else:values['activity_minutes']=round(sum(minutes))
        if sleeps:values['sleep_hours']=sleeps[0]
        old=next(iter(self.store.records_range(day,day)),{'record_date':day})
        changed=False
        with self.store.connection() as db:
            for field,value in values.items():
                projection=db.execute('SELECT value FROM personal_record_projections WHERE day=? AND field=?',(day,field)).fetchone()
                if old.get(field) is None or projection and old.get(field)==json.loads(projection['value']):
                    if old.get(field)!=value:old[field]=value;changed=True
                    db.execute('INSERT INTO personal_record_projections VALUES (?,?,?) ON CONFLICT(day,field) DO UPDATE SET value=excluded.value',(day,field,dump(value)))
        if changed:self.store.save_record(old)

    def working_context(self,session_id=None,goal_id=None):
        if session_id:self.get('session',session_id)
        if goal_id:self.get('goal',goal_id)
        goals=[g for g in self.list('goal','active') if not goal_id or g['id']==goal_id]
        tasks=[t for t in self.list('task',limit=500) if (not goal_id or t.get('goal_id')==goal_id) and t['status'] not in ('cancelled',)]
        pending=[t for t in tasks if t['status'] in ('scheduled','waiting_information','waiting_confirmation','blocked')]
        views=[]
        for t in tasks[:30]:
            view={k:t.get(k) for k in ('id','title','kind','status','start_at','end_at','goal_id','date','feedback_source_id')}
            if t.get('last_feedback'):view['last_feedback']={k:(v[:200] if isinstance(v,str) else v) for k,v in t['last_feedback'].items() if k in ('outcome','note','actual_minutes','sleep_hours')}
            views.append(view)
        progress={g['id']:{'completed_tasks':sum(t['status']=='completed' for t in tasks if t.get('goal_id')==g['id']),
                            'partial_tasks':sum(t['status']=='partial' for t in tasks if t.get('goal_id')==g['id']),
                            'observed_activity_minutes':sum(t['last_feedback']['actual_minutes'] for t in tasks if t.get('goal_id')==g['id'] and t['kind']=='activity' and t.get('last_feedback',{}).get('actual_minutes') is not None) if any(t.get('last_feedback',{}).get('actual_minutes') is not None for t in tasks if t.get('goal_id')==g['id'] and t['kind']=='activity') else None,
                            'note':'仅统计明确反馈，不用计划值填补实际值。'} for g in goals}
        return {'goals':goals[:10],'goal_progress':progress,'tasks':views,'pending_count':len(pending),
                'next_steps':[{'task_id':t['id'],'title':t['title'],'status':t['status'],'start_at':t.get('start_at')} for t in sorted(pending,key=lambda x:x.get('start_at') or '9999')[:8]],
                'recent_decisions':self.journal('decision',limit=5),'recent_feedback':self.journal('feedback',limit=5),
                'observations':self.list('observation',limit=8),'updated_at':stamp()}
