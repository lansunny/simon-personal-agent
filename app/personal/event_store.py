"""来源事件与逻辑事件分离：多个渠道更新同一事项，保留原始来源。"""
import json
from app.personal.continuity import dump,stamp


class EventStore:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS personal_events (
                    id TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_event_aliases (
                    raw_id TEXT PRIMARY KEY,canonical_id TEXT NOT NULL);
            ''')
            for row in db.execute('SELECT * FROM life_events').fetchall():
                if db.execute('SELECT 1 FROM personal_event_aliases WHERE raw_id=?',(row['id'],)).fetchone():continue
                db.execute('INSERT OR IGNORE INTO personal_events VALUES (?,?,?,?)',(row['id'],row['revision'],row['body'],row['updated_at']))
                db.execute('INSERT OR IGNORE INTO personal_event_aliases VALUES (?,?)',(row['id'],row['id']))

    def get(self,event_id):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM personal_events WHERE id=?',(event_id,)).fetchone()
        if not row:raise KeyError(event_id)
        return {**json.loads(row['body']),'id':row['id'],'revision':row['revision']}

    def list(self):
        with self.store.connection() as db:ids=[r[0] for r in db.execute('SELECT id FROM personal_events ORDER BY updated_at DESC,rowid DESC')]
        return [self.get(key) for key in ids]

    def canonical_id(self,raw_id):
        with self.store.connection() as db:row=db.execute('SELECT canonical_id FROM personal_event_aliases WHERE raw_id=?',(raw_id,)).fetchone()
        return row[0] if row else raw_id

    def resolve(self,source,external_id):
        with self.store.connection() as db:row=db.execute('SELECT id FROM life_events WHERE source=? AND external_id=?',(source,external_id)).fetchone()
        return self.get(self.canonical_id(row[0])) if row else None

    def update(self,raw_id,body):
        relation=body.get('related_event_id')
        if relation:self.get(relation)
        canonical=relation or self.canonical_id(raw_id)
        # 邀请不能覆盖已经确定的事件，只建立独立待定事项。
        if body['status']=='tentative' and relation and self.get(relation)['status']=='confirmed':canonical=raw_id
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM personal_events WHERE id=?',(canonical,)).fetchone()
            revision=old['revision']+1 if old else 1
            db.execute('INSERT INTO personal_events VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated_at=excluded.updated_at',
                       (canonical,revision,dump(body),stamp()))
            db.execute('INSERT INTO personal_event_aliases VALUES (?,?) ON CONFLICT(raw_id) DO UPDATE SET canonical_id=excluded.canonical_id',(raw_id,canonical))
            # 兼容旧提交器的事件版本检查。原始来源body保持独立，重复源同步不会覆盖用户新报告。
            db.execute('UPDATE life_events SET revision=? WHERE id=?',(revision,canonical))
        return self.get(canonical)
