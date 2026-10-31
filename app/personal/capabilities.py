"""运行时能力目录与按需技能。说明文件不能授予额外工具权限。"""
import hashlib
import json
from pathlib import Path
from pydantic import Field
from typing import Literal
from app.schemas import InputModel
from app.multi_agent.tools import ToolBinding


class SkillRead(InputModel):
    name: str = Field(min_length=1,max_length=60)
    reference: str = Field(default='',max_length=100)


class TaskQuery(InputModel):
    task_id: str = Field(default='',max_length=100)
    goal_id: str = Field(default='',max_length=100)
    limit: int = Field(default=8,ge=1,le=15)
    kind: Literal['','activity','meal','sleep','checkin','other'] = ''
    start_date: str = Field(default='',pattern=r'^$|^\d{4}-\d{2}-\d{2}$')
    end_date: str = Field(default='',pattern=r'^$|^\d{4}-\d{2}-\d{2}$')


class SkillCatalog:
    def __init__(self):
        self.root=Path(__file__).parent/'skills'
        self.catalog={
            'health-plan':{'description':'协调饮食、睡眠、运动，在日程变化后生成有依据的健康安排。','file':'health-plan.md','tools':['query_history','query_environment','read_personal_tasks']},
            'daily-planning':{'description':'接续已有目标和未完成事项，查看安排、解释调整、记录反馈。','file':'daily-planning.md','tools':['read_working_context','read_personal_tasks']},
            'evening-review':{'description':'按真实记录复盘，区分事实与假设，产生可追溯的后续问题。','file':'evening-review.md','tools':['query_history','read_personal_tasks']},
        }

    def list(self):return [{'name':name,'description':v['description']} for name,v in self.catalog.items()]

    def read(self,name,reference=''):
        if name not in self.catalog:raise ValueError('技能不在受控目录中。')
        if reference:raise ValueError('当前技能没有附加文件，不能读取任意路径。')
        record=self.catalog[name];content=(self.root/record['file']).read_text(encoding='utf-8')
        return {'name':name,'content':content,'version':hashlib.sha256(content.encode()).hexdigest()[:16],
                'requested_tools':record['tools'],'note':'工具权限仍由程序和用户授权决定。'}


class PersonalReadAccess:
    def __init__(self,continuity,session_id=None,goal_id=None,role=None):
        self.continuity=continuity;self.session_id=session_id;self.goal_id=goal_id;self.catalog=SkillCatalog();self.loaded=[]
        self.role=role

    def working_context(self):
        value=self.continuity.working_context(self.session_id,self.goal_id)
        if self.role in ('activity','diet','sleep'):
            kind={'activity':'activity','diet':'meal','sleep':'sleep'}[self.role]
            value['tasks']=[t for t in value['tasks'] if t['kind']==kind]
            ids={t['id'] for t in value['tasks']}
            value['next_steps']=[t for t in value['next_steps'] if t['task_id'] in ids]
            value['goals']=[g for g in value['goals'] if self.role in g['domains']]
            value.pop('recent_decisions',None);value.pop('recent_feedback',None);value.pop('observations',None)
        return value

    def bindings(self):
        from app.multi_agent.tools import NoArguments
        def load(name,reference=''):
            result=self.catalog.read(name,reference)
            self.loaded.append({'name':name,'version':result['version']});return result
        def tasks(task_id='',goal_id='',limit=8,kind='',start_date='',end_date=''):
            role_kind={'activity':'activity','diet':'meal','sleep':'sleep'}.get(self.role)
            if role_kind and kind and role_kind!=kind:return {'status':'denied','message':'该角色只读取本领域事项。'}
            kind=role_kind or kind
            if task_id:
                try:task=self.continuity.get('task',task_id)
                except KeyError:return {'status':'not_found','message':'未找到该事项，请先查询列表并使用返回的真实ID。'}
                if kind and task['kind']!=kind:return {'status':'denied','message':'该角色只读取本领域事项。'}
                return task
            items=[t for t in self.continuity.list('task',limit=1000) if (not kind or t['kind']==kind) and (not (goal_id or self.goal_id) or t.get('goal_id')==(goal_id or self.goal_id)) and (not start_date or str(t.get('start_at') or t.get('date') or '')[:10]>=start_date) and (not end_date or str(t.get('start_at') or t.get('date') or '')[:10]<=end_date)]
            return sorted(items,key=lambda t:t.get('start_at') or '9999')[:limit]
        return {
            'read_working_context':ToolBinding(NoArguments,self.working_context),
            'load_skill':ToolBinding(SkillRead,load),
            'read_personal_tasks':ToolBinding(TaskQuery,tasks),
        }
