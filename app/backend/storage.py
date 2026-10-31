"""Single-user business data. Graph checkpoints live in a separate SQLite file."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (day TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO meta VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
                    base_version INTEGER NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', error TEXT);
                CREATE TABLE IF NOT EXISTS plans (
                    version INTEGER PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
                    body TEXT NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_key TEXT NOT NULL,
                    operation TEXT NOT NULL, before_body TEXT, after_body TEXT, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS revisions_entity ON revisions(entity, entity_key, id);
            ''')
            # 只迁移当前可观测快照，不伪造升级前已经覆盖的历史。
            if not db.execute('SELECT 1 FROM revisions LIMIT 1').fetchone():
                old_profile = db.execute('SELECT body FROM profile WHERE id=1').fetchone()
                if old_profile:
                    db.execute('INSERT INTO revisions(entity,entity_key,operation,after_body,created_at) VALUES (?,?,?,?,?)',
                               ('profile','1','migration_snapshot',old_profile[0],datetime.now(timezone.utc).isoformat()))
                for old in db.execute('SELECT day,body FROM records').fetchall():
                    db.execute('INSERT INTO revisions(entity,entity_key,operation,after_body,created_at) VALUES (?,?,?,?,?)',
                               ('record',old[0],'migration_snapshot',old[1],datetime.now(timezone.utc).isoformat()))


    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def profile(self):
        with self.connection() as db:
            row = db.execute('SELECT body FROM profile WHERE id=1').fetchone()
            return json.loads(row['body']) if row else None

    def save_profile(self, profile):
        with self.connection() as db:
            self._audit(db, 'profile', '1', 'save', profile)
            db.execute('INSERT OR REPLACE INTO profile VALUES (1, ?)', (encode(profile),))
            db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')

    def records(self):
        with self.connection() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM records ORDER BY day')]

    def save_record(self, record):
        with self.connection() as db:
            self._audit(db, 'record', record['record_date'], 'save', record)
            db.execute('INSERT OR REPLACE INTO records VALUES (?, ?)', (record['record_date'], encode(record)))
            db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')

    def delete_record(self, day):
        with self.connection() as db:
            self._audit(db, 'record', day, 'delete', None)
            db.execute('DELETE FROM records WHERE day=?', (day,))
            db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')

    def revision(self):
        with self.connection() as db:
            return db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]

    def plans(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM plans ORDER BY version DESC')]
        for row in rows:
            row['body'] = json.loads(row['body'])
        return rows

    def active_plan(self):
        return next((p for p in self.plans() if p['active']), None)

    def revoke_plan(self):
        with self.connection() as db:
            db.execute('UPDATE plans SET active=0 WHERE active=1')
            db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')

    def create_run(self, run_id, mode, base_version, revision):
        with self.connection() as db:
            db.execute('INSERT INTO runs (id,mode,status,base_version,revision,created_at) VALUES (?,?,?,?,?,?)',
                       (run_id, mode, 'running', base_version, revision, datetime.now(timezone.utc).isoformat()))

    def runs(self):
        with self.connection() as db:
            return [r['id'] for r in db.execute('SELECT id FROM runs ORDER BY created_at DESC')]

    def run(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result['state'] = json.loads(result['state'])
        return result

    def is_current(self, run):
        active = self.active_plan()
        return (active['version'] if active else 0) == run['base_version'] and self.revision() == run['revision']

    def finish(self, run_id, state, status, error=None):
        """Idempotent activation: run_id is unique; old versions remain as history."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if status == 'approved':
                existing = db.execute('SELECT version FROM plans WHERE run_id=?', (run_id,)).fetchone()
                if existing is None:
                    current = db.execute('SELECT version FROM plans WHERE active=1').fetchone()
                    revision = db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
                    if (current[0] if current else 0) != run['base_version'] or revision != run['revision']:
                        status = 'stale'
                        error = '档案、记录或当前计划已变化，请重新生成。'
                    else:
                        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM plans').fetchone()[0]
                        db.execute('UPDATE plans SET active=0 WHERE active=1')
                        db.execute('INSERT INTO plans VALUES (?,?,?,?,?)',
                                   (version, run_id, encode(state['active_plan']), 1, datetime.now(timezone.utc).isoformat()))
            db.execute('UPDATE runs SET state=?,status=?,error=? WHERE id=?', (encode(state), status, error, run_id))
        return self.run(run_id)

    def _audit(self, db, entity, key, operation, body):
        sql = 'SELECT body FROM profile WHERE id=1' if entity == 'profile' else 'SELECT body FROM records WHERE day=?'
        before = db.execute(sql, () if entity == 'profile' else (key,)).fetchone()
        db.execute('INSERT INTO revisions(entity,entity_key,operation,before_body,after_body,created_at) VALUES (?,?,?,?,?,?)',
                   (entity, key, operation, before[0] if before else None, encode(body) if body is not None else None,
                    datetime.now(timezone.utc).isoformat()))

    def revisions(self, entity, key):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM revisions WHERE entity=? AND entity_key=? ORDER BY id', (entity, key)).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in ('before_body', 'after_body'):
                item[field] = json.loads(item[field]) if item[field] else None
        return items

    def records_range(self, start, end):
        with self.connection() as db:
            return [json.loads(row['body']) for row in db.execute('SELECT body FROM records WHERE day>=? AND day<=? ORDER BY day', (start,end))]
