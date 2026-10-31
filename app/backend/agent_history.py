"""一次角色调用的只读检索访问器；引用、缓存、额度均不跨角色共享。"""
import copy
import json
from datetime import date, timedelta
from uuid import uuid4


class AgentHistoryAccess:
    def __init__(self, memory, role, as_of, context, max_calls=3, max_days=90):
        self.memory, self.role, self.as_of = memory, role, as_of
        self.max_calls, self.max_days = max_calls, max_days
        self.current_source_id = context.get('source_message_id')
        self.memory_as_of = context.get('memory_as_of') or context.get('intent_plan', {}).get('target_date') or as_of
        self.calls = 0
        self.cache = {}
        self.evidence = []
        self.sources = set()
        self._collect_sources(context)

    def _collect_sources(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'source_message_id' and isinstance(item, str):
                    self.sources.add(item)
                elif key == 'messages' and isinstance(item, list):
                    for message in item:
                        if isinstance(message, dict) and message.get('id'):
                            self.sources.add(message['id'])
                else:
                    self._collect_sources(item)
        elif isinstance(value, list):
            for item in value:
                self._collect_sources(item)

    def invoke(self, name, arguments):
        if self.calls >= self.max_calls:
            return {'status': 'limit_reached', 'message': '补查额度已用完，请依据已有证据提交，或明确说明缺失。',
                    'remaining_calls': 0}
        self.calls += 1
        cache_key = name + ':' + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        if cache_key in self.cache:
            item = copy.deepcopy(self.cache[cache_key])
            item['cached'] = True
            item['remaining_calls'] = self.max_calls - self.calls
            return item
        try:
            if name == 'query_history':
                data = self._history(arguments)
            elif name == 'read_memories':
                data = self._memories(arguments)
            elif name == 'read_history_message':
                data = self._source(arguments)
            else:
                raise ValueError('未知只读检索工具。')
            item = {'id': 'R_' + self.role + '_' + uuid4().hex[:10], 'tool': name, 'origin_role': self.role,
                    'status': 'ok', 'data': data, 'retrieved_as_of': self.as_of,
                    'remaining_calls': self.max_calls - self.calls,
                    'message': '来自本机历史；截断片段不代表完整原文，缺少记录不等于未执行。'}
            self._collect_sources(data)
            self.evidence.append(copy.deepcopy(item))
            self.cache[cache_key] = copy.deepcopy(item)
            return item
        except (ValueError, KeyError) as exc:
            # 只返回受控错误，不暴露 sqlite/请求正文等内部异常。
            return {'status': 'invalid_query', 'message': str(exc) if isinstance(exc, ValueError) else '没有找到该来源。',
                    'remaining_calls': self.max_calls - self.calls}
        except Exception:
            return {'status':'unavailable', 'message':'历史查询暂不可用，请依据已取得证据或明确说明缺失。',
                    'remaining_calls':self.max_calls-self.calls}

    def _topic(self, requested):
        own = self.role if self.role in ('diet', 'activity', 'sleep') else None
        if own and requested not in ('all', own):
            raise ValueError('该角色不能请求另一个工作角色的专属主题。')
        return own or requested

    @staticmethod
    def _excerpt(value, max_chars=300):
        if value is None:
            return value
        return value[:max_chars] if isinstance(value, str) else value

    def _history(self, q):
        topic = self._topic(q['topic'])
        limit, cursor = q['limit'], q['cursor']
        if q['kind'] == 'records':
            end = q['end_date'] or q['start_date'] or self.as_of
            start = q['start_date'] or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            if not 1 <= span <= self.max_days:
                raise ValueError(f'单次补查日期范围必须为1至{self.max_days}天。')
            if cursor:
                date.fromisoformat(cursor)
            rows = self.memory.store.records_range(start, end)
            eligible = [r for r in rows if not cursor or r['record_date'] > cursor]
            selected = eligible[:limit]
            fields = {'record_date', 'feedback'}
            fields |= {'meal_notes'} if topic == 'diet' else {'activity_minutes', 'sleep_hours'} if topic == 'activity' else {'sleep_hours','sleep_quality','fatigue'} if topic=='sleep' else {
                'weight_kg', 'activity_minutes', 'sleep_hours', 'meal_notes'}
            records = []
            for row in selected:
                record = {k: self._excerpt(v) for k, v in row.items() if k in fields}
                record['excerpted_fields'] = [k for k in fields if isinstance(row.get(k), str) and len(row[k]) > 300]
                records.append(record)
            summary = copy.deepcopy(self.memory.summarize(start, end))
            summary['feedback'] = [{'date': f['date'], 'text': self._excerpt(f['text'])}
                                   for f in summary['feedback'][:limit]]
            if topic == 'diet':
                for key in ('mean_weight_kg', 'first_to_last_weight_change_kg', 'activity_minutes_observed', 'activity_recorded_days'):
                    summary.pop(key, None)
            revisions = self.memory.store.revisions('record', start) if q['include_revisions'] and start == end else []
            revision_views = []
            for raw in revisions[-limit:]:
                item = {k:v for k,v in raw.items() if k not in ('before_body','after_body')}
                for key in ('before_body', 'after_body'):
                    item[key] = {k:self._excerpt(v) for k,v in (raw[key] or {}).items() if k in fields} if raw[key] else None
                revision_views.append(item)
            return {'kind': 'records', 'topic': topic, 'start_date': start, 'end_date': end,
                    'summary': summary, 'records': records, 'revision_history': revision_views,
                    'revision_count': len(revisions), 'total': len(rows), 'returned': len(records),
                    'next_cursor': selected[-1]['record_date'] if len(eligible) > limit and selected else None,
                    'source_revision': self.memory.store.revision(), 'source': 'business.sqlite3'}
        if q['kind'] == 'messages':
            if q['start_date'] or q['end_date']:
                raise ValueError('对话检索使用关键词与 cursor，日期范围仅用于记录查询。')
            selected = self.memory.messages(q['query'], limit + 1, cursor or None, exclude_id=self.current_source_id)
            page = selected[:limit]
            views = [{**m, 'content': m['content'][:300], 'content_chars': len(m['content']),
                      'excerpted': len(m['content']) > 300} for m in page]
            return {'kind': 'messages', 'topic': topic, 'messages': views, 'returned': len(views),
                    'next_cursor': page[-1]['id'] if len(selected) > limit and page else None,
                    'matching': '关键词包含查询；topic 不提供完整的对话语义分类。'}
        if q['kind'] == 'plans':
            if q['start_date'] or q['end_date']:
                raise ValueError('日期范围仅用于记录查询，计划查询请使用版本号。')
            if cursor and not cursor.isdigit():
                raise ValueError('计划 cursor 必须为版本号。')
            rows = [p for p in self.memory.store.plans() if (q['version'] is None or p['version'] == q['version'])
                    and (not cursor or p['version'] < int(cursor))]
            selected = rows[:min(limit, 3)]
            views = []
            for plan in selected:
                body = plan['body']
                keys = {'rationale', 'changes', 'planning_context'}
                keys |= {'diet_suggestions', 'dining_recommendations'} if topic == 'diet' else {
                    'activity_days', 'minutes_per_session', 'activity_suggestions', 'daily_schedule'} if topic == 'activity' else set(body)
                content = {k:copy.deepcopy(body[k]) for k in keys if k in body}
                for day in content.get('daily_schedule', []):
                    day.pop('route_steps', None)
                    day.pop('return_route_steps', None)
                views.append({**{k:v for k,v in plan.items() if k != 'body'}, 'body': content})
            return {'kind': 'plans', 'topic': topic, 'plans': views, 'total': len(rows), 'returned': len(views),
                    'next_cursor': str(selected[-1]['version']) if len(rows) > len(selected) and selected else None,
                    'details_note': '省略地图转向，完整版本仍可在历史计划接口读取。'}
        raise ValueError('不支持的历史查询类型。')

    def _memories(self, q):
        topic = self._topic(q['topic'])
        rows = self.memory.entries(self.memory_as_of, limit=200) if q['include_history'] else self.memory.recall(self.memory_as_of)
        rows = [r for r in rows if topic == 'all' or r.get('topic') == topic]
        return {'memories': rows[:q['limit']], 'returned': min(len(rows), q['limit']), 'available': len(rows),
                'as_of': self.memory_as_of, 'include_history': q['include_history'],
                'note': '过期、撤销、被替代的记忆仅作历史，不能当成当前约束。'}

    def _source(self, q):
        if q['message_id'] not in self.sources:
            raise ValueError('只允许读取初始记忆或补查结果中已经出现的消息来源 ID，请先 query_history。')
        message = self.memory.source(q['message_id'])
        text, start = message['content'], q['offset']
        if start > len(text):
            raise ValueError('offset 超过原文长度。')
        end = min(len(text), start + q['max_chars'])
        return {'message': {**message, 'content': text[start:end]}, 'offset': start, 'content_chars': len(text),
                'next_offset': end if end < len(text) else None, 'is_complete': start == 0 and end == len(text),
                'note': 'is_complete表示单次是否为完整消息；多个片段须从0连续读取到next_offset为空，才能组合成完整原文。'}
