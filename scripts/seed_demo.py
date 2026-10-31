"""创建独立Demo数据目录，包含示例档案、计划与日程事件。"""
import argparse
from datetime import datetime,timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.backend.service import Service
from app.personal.agent import HealthPersonalAgent
from app.personal.contracts import PersonalChatInput,LifeEvent
from app.personal.conversation import chat


def seed(directory):
    directory=Path(directory).resolve()
    if directory.exists() and any(directory.glob('*.sqlite3')):
        raise ValueError('该目录已有数据库，请指定新的演示目录；不会覆盖现有记录。')
    now=datetime.now(ZoneInfo('Asia/Shanghai'));today=now.date()
    service=Service(directory);agent=HealthPersonalAgent(service,start_worker=False);service.personal=agent
    try:
        service.store.save_profile({'age':25,'height_cm':175,'baseline_weight_kg':70,'baseline_date':today.isoformat(),
            'goal':'build_habits','available_activity_days':3,'minutes_per_session':20,
            'sleep_bedtime':'23:00','sleep_wake_time':'07:00','sleep_target_hours':8,'timezone_name':'Asia/Shanghai',
            'food_preferences':['清淡口味'],'activity_constraints':[],'needs_professional_support':False})
        for offset,minutes,sleep in [(-6,20,7.5),(-5,0,8),(-3,25,7),(-2,15,7.5),(0,20,8)]:
            service.store.save_record({'record_date':(today+timedelta(days=offset)).isoformat(),'activity_minutes':minutes,'sleep_hours':sleep})
        session=agent.continuity.session('Simon展示演示')
        agent.continuity.put('session',session['id'],{**session,'demo_fixture':True})
        goal=agent.continuity.create_goal({'title':'让工作日留一点自己的时间','objective':'结合日程协调运动、饮食和休息，以实际反馈调整。',
            'domains':['activity','diet','sleep'],'session_id':session['id']})
        # 首次打开网页即可复用这份演示会话；普通数据目录没有这条标记。
        agent.continuity.put('demo_session','current',{'session_id':session['id']})
        run=service.generate('demo','每周三次，晚上19:00在家室内散步，每次20分钟。制定运动、饮食和睡眠计划。',timezone_offset=-480)
        if run['status']!='waiting_confirmation':raise RuntimeError('Demo计划未进入待确认状态：'+run['status'])
        agent.continuity.link_run(session['id'],run['id'],goal['id']);service.decide(run['id'],True)
        first=next(day for day in service.store.active_plan()['body']['daily_schedule'] if day['kind']=='exercise')
        start=datetime.fromisoformat(first['start_at']);end=start+timedelta(hours=1)
        text=f'{start:%Y-%m-%d} {start:%H:%M}到{end:%H:%M}有线上会议，帮我调整原来的散步。'
        event=agent.ingest(LifeEvent(source='manual',external_id='demo-meeting',title='演示：临时线上会议',kind='meeting',
            start_at=start,end_at=end,source_text=text).model_dump(mode='json'),mode='demo')
        job=agent.runtime.run_now(event['job']['id'])
        if job['status']!='succeeded':raise RuntimeError('Demo事件任务未完成。')
        action=next(a for a in agent.actions() if a['event_id']==event['event_id'])
        agent.continuity.message(session['id'],'user',text)
        agent.continuity.message(session['id'],'assistant','会议和原来的活动时间有冲突。我保留20分钟活动，整理了新的时间候选，你可以先看差异再决定。')
        return {'directory':str(directory),'mode':'demo','session_id':session['id'],'action_status':action['status'],'tasks':len(agent.continuity.list('task'))}
    finally:agent.close();service.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data'/'demo')
    args=parser.parse_args()
    result=seed(args.data_dir)
    print('Demo数据目录：',result['directory'])
    print('请将HENGBAN_DATA_DIR设为该目录并启动run.py；网页中选择离线演示。')
