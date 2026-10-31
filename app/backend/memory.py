"""跨任务记忆仓储。原始历史、有效事实、派生摘要分别保存；不依赖 checkpoint。"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.multi_agent.memory_contracts import MemoryProposal, HistoryQuery

LOCAL_USER = 'local_user'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class MemoryManager:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, run_id TEXT,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS messages_user_time ON messages(user_id, created_at);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, topic TEXT NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, scope TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT,
                    status TEXT NOT NULL, source_message_id TEXT NOT NULL, reason TEXT NOT NULL,
                    supersedes TEXT, base_revision INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memories_user_key ON memories(user_id, key, status);
                CREATE TABLE IF NOT EXISTS memory_key_versions (
                    user_id TEXT NOT NULL, key TEXT NOT NULL, scope TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(user_id, key, scope));
                CREATE TABLE IF NOT EXISTS memory_summaries (
                    user_id TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                    revision INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, start_date, end_date));
            ''')

        self._backfill_dialogue()

    def _backfill_dialogue(self):
        with self.store.connection() as db:
            if db.execute("SELECT 1 FROM memory_meta WHERE key='legacy_dialogue'").fetchone():
                return
            for run in db.execute('SELECT id,state FROM runs ORDER BY created_at').fetchall():
                state = json.loads(run['state'])
                turns = state.get('dialogue') or ([{'role':'user','content':state['user_message']}] if state.get('user_message') else [])
                for i, turn in enumerate(turns):
                    if turn.get('role') in ('user','assistant') and turn.get('content'):
                        db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',
                                   (uuid4().hex,LOCAL_USER,run['id'],turn['role'],turn['content'],utc_now(),f"legacy:{run['id']}:{i}"))
            db.execute("INSERT INTO memory_meta VALUES ('legacy_dialogue','snapshot_only')")

    def message(self, role, content, run_id=None, dedupe_key=None):
        if not content:
            return None
        message_id = uuid4().hex
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',
                       (message_id, LOCAL_USER, run_id, role, content, utc_now(), dedupe_key))
            if dedupe_key:
                return db.execute('SELECT id FROM messages WHERE dedupe_key=?', (dedupe_key,)).fetchone()[0]
        return message_id

    def source(self, message_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM messages WHERE id=? AND user_id=?', (message_id, LOCAL_USER)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return dict(row)

    def agent_access(self, role, as_of, context):
        from app.backend.agent_history import AgentHistoryAccess
        from app.config import settings
        return AgentHistoryAccess(self, role, as_of, context, settings.agent_max_retrieval_calls,
                                  settings.agent_history_max_days)

    def link_message(self, message_id, run_id):
        with self.store.connection() as db:
            db.execute('UPDATE messages SET run_id=? WHERE id=? AND user_id=?', (run_id, message_id, LOCAL_USER))

    def messages(self, query='', limit=20, before=None, exclude_id=None):
        clauses, args = ['user_id=?'], [LOCAL_USER]
        if query:
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append("content LIKE ? ESCAPE '\\'")
            args.append('%' + escaped + '%')
        if exclude_id:
            clauses.append('id<>?')
            args.append(exclude_id)
        if before:
            clauses.append('rowid < (SELECT rowid FROM messages WHERE id=? AND user_id=?)')
            args.extend([before, LOCAL_USER])
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM messages WHERE ' + ' AND '.join(clauses) +
                              ' ORDER BY rowid DESC LIMIT ?', (*args, min(max(limit, 1), 100))).fetchall()
        return [dict(row) for row in rows]

    def entries(self, as_of=None, status=None, limit=100):
        as_of = as_of or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        # 有效期在读取时判断，避免没有后台定时器时遗忘到期条件。
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM memories WHERE user_id=? ORDER BY rowid DESC LIMIT ?',
                              (LOCAL_USER, min(limit, 200))).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item['value'] = json.loads(item['value'])
            if item['status'] == 'active' and item['valid_until'] and item['valid_until'] < as_of:
                item['effective_status'] = 'expired'
            elif item['status'] == 'active' and item['valid_from'] > as_of:
                item['effective_status'] = 'scheduled'
            else:
                item['effective_status'] = item['status']
            if status is None or item['effective_status'] == status:
                output.append(item)
        return output

    def recall(self, as_of, topics=None):
        # SQL 先过滤生效范围，不让大量过期/待确认条目挤掉有效事实。
        with self.store.connection() as db:
            rows = db.execute('''SELECT * FROM memories WHERE user_id=? AND status='active'
                AND valid_from<=? AND (valid_until IS NULL OR valid_until>=?) ORDER BY rowid DESC''',
                              (LOCAL_USER, as_of, as_of)).fetchall()
        chosen, seen = [], set()
        # 同一 key 的短期条件优先，过期后长期条目仍然存在。
        items = [dict(row) for row in rows]
        items.sort(key=lambda item: item['scope'] != 'temporary')
        for item in items:
            if item['key'] in seen or (topics and item['topic'] not in topics):
                continue
            seen.add(item['key'])
            chosen.append({**item, 'value': json.loads(item['value'])})
        return chosen[:12]

    def propose(self, proposal, source_message_id):
        proposal = MemoryProposal.model_validate(proposal).model_dump()
        with self.store.connection() as db:
            source = db.execute("SELECT content FROM messages WHERE id=? AND user_id=? AND role='user'",
                                (source_message_id, LOCAL_USER)).fetchone()
            if not source:
                raise ValueError('记忆必须引用已存档的用户消息。')
            existing = db.execute('''SELECT id FROM memories WHERE user_id=? AND source_message_id=?
                AND key=? AND value=? AND scope=? AND valid_from=? AND COALESCE(valid_until,'')=?''',
                                 (LOCAL_USER, source_message_id, proposal['key'], dump(proposal['value']),
                                  proposal['scope'], proposal['valid_from'], proposal['valid_until'] or '')).fetchone()
            if existing:
                return existing[0]
            memory_id = uuid4().hex
            version = db.execute('SELECT revision FROM memory_key_versions WHERE user_id=? AND key=? AND scope=?',
                                 (LOCAL_USER, proposal['key'], proposal['scope'])).fetchone()
            db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (memory_id, LOCAL_USER, proposal['key'].split('.')[0], proposal['key'], dump(proposal['value']),
                        proposal['scope'], proposal['valid_from'], proposal['valid_until'], 'pending', source_message_id,
                        proposal['reason'], None, version[0] if version else 0, utc_now()))
        return memory_id

    def decide(self, memory_id, approved, as_of):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM memories WHERE id=? AND user_id=?', (memory_id, LOCAL_USER)).fetchone()
            if not row:
                raise KeyError(memory_id)
            if row['status'] != 'pending':
                if (approved and row['status']=='rejected') or (not approved and row['status']!='rejected'):
                    raise ValueError('记忆已处理，不能改变此前的确认决定。')
                item = dict(row)
                item['value'] = json.loads(item['value'])
                return item
            current = db.execute('SELECT revision FROM memory_key_versions WHERE user_id=? AND key=? AND scope=?',
                                 (LOCAL_USER, row['key'], row['scope'])).fetchone()
            revision = current[0] if current else 0
            if approved and row['base_revision'] != revision:
                raise ValueError('记忆候选依据已变化，请重新提交记忆，避免旧候选覆盖新偏好。')
            if approved and row['valid_until'] and row['valid_until'] < as_of:
                raise ValueError('这条临时条件已经过期，请重新描述。')
            supersedes = None
            if approved:
                previous = db.execute("SELECT id FROM memories WHERE user_id=? AND key=? AND scope=? AND status='active' ORDER BY rowid DESC",
                                      (LOCAL_USER, row['key'], row['scope'])).fetchall()
                supersedes = previous[0]['id'] if previous else None
                # 长期与短期分别替代；临时条件到期后可回落到长期偏好。
                for old in previous:
                    old_row = db.execute('SELECT valid_from,valid_until FROM memories WHERE id=?', (old['id'],)).fetchone()
                    if row['valid_from'] > as_of and old_row['valid_from'] < row['valid_from']:
                        until = (date.fromisoformat(row['valid_from']) - timedelta(days=1)).isoformat()
                        if old_row['valid_until']:
                            until = min(until, old_row['valid_until'])
                        db.execute('UPDATE memories SET valid_until=? WHERE id=?', (until, old['id']))
                    else:
                        db.execute("UPDATE memories SET status='superseded' WHERE id=?", (old['id'],))
                db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
                db.execute('INSERT INTO memory_key_versions VALUES (?,?,?,1) ON CONFLICT(user_id,key,scope) DO UPDATE SET revision=revision+1',
                           (LOCAL_USER, row['key'], row['scope']))
            db.execute('UPDATE memories SET status=?, supersedes=? WHERE id=?',
                       ('active' if approved else 'rejected', supersedes, memory_id))
        with self.store.connection() as db:
            item = dict(db.execute('SELECT * FROM memories WHERE id=? AND user_id=?', (memory_id, LOCAL_USER)).fetchone())
        item['value'] = json.loads(item['value'])
        return item

    def revoke(self, memory_id):
        with self.store.connection() as db:
            row = db.execute('SELECT key,scope,status FROM memories WHERE id=? AND user_id=?', (memory_id, LOCAL_USER)).fetchone()
            changed = db.execute("UPDATE memories SET status='revoked' WHERE id=? AND user_id=? AND status IN ('active','pending')",
                                 (memory_id, LOCAL_USER)).rowcount
            if changed:
                db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
                if row['status'] == 'active':
                    db.execute('INSERT INTO memory_key_versions VALUES (?,?,?,1) ON CONFLICT(user_id,key,scope) DO UPDATE SET revision=revision+1',
                               (LOCAL_USER,row['key'],row['scope']))
        return bool(changed)

    def profile(self, profile, memories):
        effective = dict(profile)
        mapping = {'diet.preferences': 'food_preferences', 'activity.constraints': 'activity_constraints',
                   'activity.minutes': 'minutes_per_session', 'activity.days': 'available_activity_days',
                   'sleep.bedtime':'sleep_bedtime','sleep.wake_time':'sleep_wake_time','sleep.target_hours':'sleep_target_hours'}
        for item in memories:
            if item['key'] in mapping:
                field = mapping[item['key']]
                effective[field] = list(dict.fromkeys(effective.get(field, []) + item['value'])) if field == 'activity_constraints' else item['value']
        return effective

    def summarize(self, start, end):
        revision = self.store.revision()
        with self.store.connection() as db:
            cached = db.execute('SELECT * FROM memory_summaries WHERE user_id=? AND start_date=? AND end_date=?',
                                (LOCAL_USER, start, end)).fetchone()
            if cached and cached['revision'] == revision:
                return json.loads(cached['body'])
        rows = self.store.records_range(start, end)
        weights = [r['weight_kg'] for r in rows if r.get('weight_kg') is not None]
        minutes = [r['activity_minutes'] for r in rows if r.get('activity_minutes') is not None]
        body = {'start_date': start, 'end_date': end, 'recorded_days': len(rows), 'weight_samples': len(weights),
                'mean_weight_kg': round(sum(weights) / len(weights), 2) if weights else None,
                'first_to_last_weight_change_kg': round(weights[-1] - weights[0], 2) if len(weights) > 1 else None,
                'activity_recorded_days': len(minutes), 'activity_minutes_observed': sum(minutes) if minutes else None,
                'feedback': [{'date': r['record_date'], 'text': r['feedback']} for r in rows if r.get('feedback')][:20],
                'source_dates': [r['record_date'] for r in rows], 'source_revision': revision,
                'note': '程序统计摘要；缺失不计零，体重首末差不代表脂肪变化。'}
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO memory_summaries VALUES (?,?,?,?,?,?)',
                       (LOCAL_USER, start, end, revision, dump(body), utc_now()))
        return body

    def history(self, query, as_of):
        q = HistoryQuery.model_validate(query)
        data = {}
        if q.kind == 'records':
            start, end = q.start_date or as_of, q.end_date or q.start_date or as_of
            rows = self.store.records_range(start, end)
            revisions = self.store.revisions('record', start) if start == end else []
            data = {'summary': self.summarize(start, end), 'records': rows[:20], 'total': len(rows),
                    'revision_history': revisions[-20:], 'revision_count': len(revisions),
                    'truncated': len(rows) > 20 or len(revisions) > 20}
        elif q.kind == 'messages':
            data = {'messages': self.messages(q.query, 8), 'matching': '关键词包含查询，未采用向量相似度。'}
        elif q.kind == 'plans':
            plans = self.store.plans()
            hits = [p for p in plans if q.version is None or p['version'] == q.version]
            data = {'plans': hits[:3], 'total': len(hits)}
        elif q.kind == 'memories':
            data = {'memories': self.entries(as_of, limit=100)}
        return {'tool': 'history', 'status': 'ok', 'data': data, 'message': '来自本机保存的数据；没有记录不等于未执行。'}

