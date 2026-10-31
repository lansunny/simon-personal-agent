"""Application boundary: snapshots -> graph -> durable draft -> explicit decision."""
import sqlite3
from pathlib import Path
from threading import RLock
from uuid import uuid4
from datetime import date, datetime, timezone, timedelta

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.multi_agent.runners import DeepSeekRunner, DemoRunner
from app.multi_agent.runners.deepseek import ModelCallError
from app.backend.storage import Store
from app.backend.memory import MemoryManager
from app.multi_agent.memory_parsing import explicit_proposals
from app.multi_agent.context_builder import ContextBudgetError
from app.multi_agent.graph import build_graph
from app.errors import BudgetExceeded


class Conflict(Exception):
    pass


class Service:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.store = Store(directory / 'business.sqlite3')
        self.memory = MemoryManager(self.store)
        self.lock = RLock()  # One local user, one server process; workers inside a graph still run in parallel.
        self.connection = sqlite3.connect(directory / 'checkpoints.sqlite3', check_same_thread=False)
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.graphs = {}
        self.active_runs=set()
        self.environment_cache = None
        # Reading/resuming a finished confirm node needs no model call or API key.
        self.reader = build_graph(DemoRunner(), self.saver, self.memory)
        with self.lock:
            for run_id in self.store.runs():
                self.get_run(run_id)

    def close(self):
        self.connection.close()

    @staticmethod
    def config(run_id):
        return {'configurable': {'thread_id': run_id}, 'recursion_limit': 40, 'max_concurrency': 3}

    def graph(self, mode):
        if mode not in self.graphs:
            personal=getattr(self,'personal',None)
            runner=personal._runner(mode) if personal else DemoRunner() if mode=='demo' else DeepSeekRunner()
            self.graphs[mode] = build_graph(runner,self.saver,self.memory,capability_factory=personal.capability_bindings if personal else None,external_caller=personal.call_external if personal else None,meal_templates_provider=personal.meal_templates if personal else None)
        return self.graphs[mode]

    def get_run(self, run_id):
        with self.lock:
            run = self.store.run(run_id)
            if run_id in self.active_runs:
                snapshot=self.reader.get_state(self.config(run_id))
                return {**run,'state':dict(snapshot.values or run['state']),'status':'running','reply':'任务正在执行，可以继续查看其他事项。'}
            if run['status'] not in ('running', 'waiting_confirmation', 'waiting_clarification'):
                return self._present(run)
            if run['status'] in ('waiting_confirmation','waiting_clarification') and not self.store.is_current(run):
                return self._present(self.store.finish(run_id, run['state'], 'stale', '档案、记忆、记录或当前计划已变化，请按最新条件重新生成。'))
            snapshot = self.reader.get_state(self.config(run_id))
            if not snapshot.values:
                return self._present(self.store.finish(run_id, {}, 'failed', '任务在写入检查点前中止，请重新生成。'))
            state = dict(snapshot.values)
            if any(task.interrupts for task in snapshot.tasks):
                status = 'waiting_clarification' if snapshot.next == ('clarify_task',) else 'waiting_confirmation'
            elif snapshot.next:
                return self._present(self.store.finish(run_id, state, 'failed', '生成过程未完成，请重新生成；此版只恢复待澄清或待确认任务。'))
            else:
                status = state.get('status', 'failed')
            return self._present(self.store.finish(run_id, state, status))

    def generate(self, mode, user_message='', city='', use_external=False, location=None, timezone_offset=0, conversation_id=None, source_message_id=None):
        with self.lock:
            profile = self.store.profile()
            if profile is None:
                raise Conflict('请先保存用户档案。')
            graph = self.graph(mode)
            active = self.store.active_plan()
            history = []
            if conversation_id:
                history = list(self.store.run(conversation_id)['state'].get('dialogue', []))[-10:]
            local_now = datetime.now(timezone(timedelta(minutes=-timezone_offset)))
            base_profile = dict(profile)
            memories = self.memory.recall(local_now.date().isoformat())
            profile = self.memory.profile(profile, memories)
            source_message_id = source_message_id or self.memory.message('user', user_message)
            run_id = uuid4().hex
            self.store.create_run(run_id, mode, active['version'] if active else 0, self.store.revision())
            self.memory.link_message(source_message_id, run_id)
            start = (local_now.date() - timedelta(days=6)).isoformat()
            personal=getattr(self,'personal',None)
            initial = {'quantitative_records':self.store.records_range((local_now.date()-timedelta(days=90)).isoformat(),local_now.date().isoformat()), 'life_events':[{k:v for k,v in e.items() if k!='source_text'} for e in personal.events() if e['status']=='confirmed' and datetime.fromisoformat(e['end_at'])>local_now and datetime.fromisoformat(e['start_at'])<local_now+timedelta(days=8)] if personal else [], 'automation_policy':personal.policy() if personal else {}, 'profile': profile, 'base_profile': base_profile, 'records': self.store.records_range(start, local_now.date().isoformat()),
                       'memory_context': memories, 'source_message_id': source_message_id,
                       'prefetched_environment': self.environment_cache if location and self.environment_cache and
                           self.environment_cache['location'] == location and
                           (datetime.now(timezone.utc) - datetime.fromisoformat(self.environment_cache['fetched_at'])).total_seconds() < 600 else None,
                       'user_message': user_message, 'dialogue': history + ([{'role': 'user', 'content': user_message}] if user_message else []),
                       'task_city': city, 'use_external': use_external, 'clarification_round': 0,
                       'previous_plan': active['body'] if active else None,
                       'mode': mode, 'as_of': local_now.date().isoformat(), 'local_now': local_now.isoformat(), 'location': location,
                       'events': [], 'coordination_round': 0}
            self.active_runs.add(run_id)
        try:
            graph.invoke(initial,self.config(run_id))
        except Exception as exc:
            with self.lock:
                snapshot=self.reader.get_state(self.config(run_id))
                failed_state=dict(snapshot.values or {})
                if isinstance(exc,BudgetExceeded):failed_state['failure']=exc.details()
                self.store.finish(run_id,failed_state,'failed',str(exc) if isinstance(exc,(ModelCallError,ContextBudgetError,BudgetExceeded)) else f'生成失败（{type(exc).__name__}），请检查输入和配置。')
        finally:
            with self.lock:self.active_runs.discard(run_id)
        return self.get_run(run_id)

    def reply(self,run_id,message,city='',expected_round=0,location=None,use_external=None,source_message_id=None,refresh_location=False):
        with self.lock:
            run=self.get_run(run_id)
            if run['status']!='waiting_clarification':raise Conflict('此任务当前不在等待补充信息。')
            if run['state'].get('clarification_round',0)!=expected_round:raise Conflict('此问题已经处理，请刷新。')
            if not self.store.is_current(run):raise Conflict('输入或当前计划已变化，请重新生成。')
            graph=self.graph(run['mode'])
            zone=datetime.fromisoformat(run['state']['local_now']).tzinfo if run['state'].get('local_now') else timezone.utc
            now=datetime.now(zone)
            source_message_id=source_message_id or self.memory.message('user',message,run_id)
            self.memory.link_message(source_message_id,run_id)
            start=(now.date()-timedelta(days=6)).isoformat()
            update={'local_now':now.isoformat(),'as_of':now.date().isoformat(),'source_message_id':source_message_id,
                    'records':self.store.records_range(start,now.date().isoformat()),'memory_context':self.memory.recall(now.date().isoformat())}
            if use_external is not None:update['use_external']=use_external
            if refresh_location:update['location']=location
            if (refresh_location or location) and location!=run['state'].get('location'):update['prefetched_environment']=None
            self.active_runs.add(run_id)
        try:graph.invoke(Command(resume={'message':message,'city':city,'location':location},update=update),self.config(run_id))
        except Exception as exc:
            with self.lock:
                snapshot=self.reader.get_state(self.config(run_id))
                failed_state=dict(snapshot.values or {})
                if isinstance(exc,BudgetExceeded):failed_state['failure']=exc.details()
                self.store.finish(run_id,failed_state,'failed',str(exc) if isinstance(exc,(ModelCallError,ContextBudgetError,BudgetExceeded)) else '恢复任务未完成，请重新生成。')
        finally:
            with self.lock:self.active_runs.discard(run_id)
        return self.get_run(run_id)

    def decide(self, run_id, approved, source_message_id=None):
        with self.lock:
            source_message_id = source_message_id or self.memory.message('user', '确认候选计划' if approved else '拒绝候选计划', run_id)
            self.memory.link_message(source_message_id, run_id)
            run = self.get_run(run_id)
            if run['status'] in ('approved', 'rejected'):
                if (run['status'] == 'approved') != approved:
                    raise Conflict('任务已经结束，不能改变此前的决定。')
                return run
            if run['status'] != 'waiting_confirmation':
                raise Conflict('此任务不处于待确认状态。')
            if approved and not self.store.is_current(run):
                self.store.finish(run_id, run['state'], 'stale', '输入或当前计划已变化，请重新生成。')
                raise Conflict('候选计划已过期，请按最新档案和记录重新生成。')
            if approved and any(datetime.fromisoformat(item['start_at']) <= datetime.now(timezone.utc)
                                for item in run['state'].get('proposal', {}).get('daily_schedule', []) if item.get('kind') == 'exercise'):
                raise Conflict('候选日程已经包含过去的运动时间，请在聊天中要求按当前时间重新安排。')
            # Only confirm is resumed here. Other unfinished nodes are never run with the demo reader.
            snapshot = self.reader.get_state(self.config(run_id))
            if snapshot.next != ('confirm',):
                raise Conflict('检查点不是确认节点，请重新生成。')
            self.reader.invoke(Command(resume={'approved': approved}), self.config(run_id))
            return self.get_run(run_id)

    def _present(self, run):
        state = run.get('state') or {}
        source = state.get('source_message_id')
        for update in state.get('intent_plan', {}).get('memory_updates', []):
            if source:
                try:
                    self.memory.propose(update, source)
                except ValueError:
                    pass  # 不合法记忆不能破坏规划任务，也不能绕过确认。
        status = run['status']
        if run.get('error'):
            reply = run['error']
        elif status == 'waiting_clarification':
            reply = state.get('clarification_question', '请补充本次需求。')
        elif state.get('answer'):
            reply = state['answer']['text']
        elif status == 'waiting_confirmation':
            plan = state.get('proposal') or {}
            if state.get('intent_plan', {}).get('dining_mode') == 'eat_out' and 'activity' not in state.get('selected_workers', []):
                reply = '已整理餐馆候选和点餐建议，确认后纳入计划。' if plan.get('dining_recommendations') else plan.get('dining_note', '已整理候选饮食建议。')
            else:
                reply = f"候选安排已完成：每周 {plan.get('activity_days', 0)} 天，每次 {plan.get('minutes_per_session', 0)} 分钟。请查看右侧安排，提出修改或确认。"
        elif status == 'approved':
            personal=getattr(self,'personal',None)
            if personal:
                active=self.store.active_plan()
                if active and active.get('run_id')==run['id']:
                    try:link=personal.continuity.get('run_link',run['id'])
                    except KeyError:link={}
                    personal.sync_state(link.get('goal_id'))
            reply = '计划已确认生效，可以继续记录、调整，或开启提醒。'
        elif status == 'rejected':
            reply = '候选未采用，原计划继续保留。'
        elif status == 'needs_manual_revision':
            reply = '方案返工后仍有约束冲突，请调整需求。'
        else:
            reply = state.get('analysis', {}).get('reason') or '任务已更新。'
        run['reply'] = reply
        if state:
            self.memory.message('assistant', reply, run['id'],
                                f"run:{run['id']}:{status}:{state.get('clarification_round', 0)}")
        return run

    def simple_reply(self, text):
        self.memory.message('assistant', text)
        return {'reply': text}

    def memory_command(self, message, as_of, source_message_id):
        # 查询和纯记忆命令由普通代码处理，避免为保存偏好调用整套规划角色。
        if message.strip() in ('查看记忆', '查看我的记忆', '查看我的偏好'):
            entries = self.memory.entries(as_of)
            active = [m for m in entries if m['effective_status'] == 'active']
            text = '当前有效记忆：' + ('；'.join(f"{m['key']}={m['value']}" for m in active) if active else '暂无。')
            return self.simple_reply(text)
        if not message.startswith(('记住', '请记住', '以后', '长期')):
            return None
        if any(word in message for word in ('帮我安排','制定','调整','规划','推荐','生成计划')):
            return None
        candidates = explicit_proposals(message, as_of)
        if not candidates:
            return None
        ids = [self.memory.propose(item, source_message_id) for item in candidates]
        text = '已提取记忆候选，请在“偏好与连接 → 记忆管理”确认。确认后会跨任务使用，长期档案表保持独立。'
        return {**self.simple_reply(text), 'memory_candidate_ids': ids}
