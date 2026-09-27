"""Local SQLite archive. No model or network access."""
import sqlite3
import json
from contextlib import closing
from pathlib import Path


class Archive:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, bot TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, timestamp REAL NOT NULL,
                    sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                    content TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(platform, bot, group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS scope_time ON messages
                    (platform, bot, group_id, timestamp, id);
                CREATE INDEX IF NOT EXISTS expiry ON messages(timestamp);
                CREATE TABLE IF NOT EXISTS summary_checkpoints (
                    platform TEXT NOT NULL, bot TEXT NOT NULL, group_id TEXT NOT NULL,
                    reader_id TEXT NOT NULL, checkpoint REAL NOT NULL,
                    PRIMARY KEY(platform, bot, group_id, reader_id)
                );
                CREATE TABLE IF NOT EXISTS media_payloads (
                    platform TEXT, bot TEXT, group_id TEXT, message_id TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(platform,bot,group_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS media_cache (
                    platform TEXT, bot TEXT, group_id TEXT, message_id TEXT,
                    provider TEXT, result TEXT NOT NULL,
                    PRIMARY KEY(platform,bot,group_id,message_id,provider)
                );
                CREATE TABLE IF NOT EXISTS auto_image_cache (
                    platform TEXT, bot TEXT, group_id TEXT, message_id TEXT,
                    image_index INTEGER, provider TEXT, result TEXT NOT NULL,
                    PRIMARY KEY(platform,bot,group_id,message_id,image_index,provider)
                );
                CREATE TABLE IF NOT EXISTS group_aliases (
                    group_id TEXT PRIMARY KEY, display_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_summary_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, bot TEXT NOT NULL, group_id TEXT NOT NULL,
                    reader_id TEXT NOT NULL, private_umo TEXT NOT NULL,
                    hour INTEGER NOT NULL, minute INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_date TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT '待运行',
                    last_sent_at REAL,
                    created_at REAL NOT NULL,
                    initial_lower REAL NOT NULL,
                    cursor_id INTEGER NOT NULL DEFAULT 0,
                    read_start_minute INTEGER,
                    read_end_minute INTEGER,
                    read_end_inclusive INTEGER NOT NULL DEFAULT 0,
                    schedule_kind TEXT NOT NULL DEFAULT 'daily',
                    date_offset INTEGER NOT NULL DEFAULT 0,
                    read_date TEXT NOT NULL DEFAULT '',
                    send_date TEXT NOT NULL DEFAULT '',
                    delivery_kind TEXT NOT NULL DEFAULT 'qq',
                    email_to TEXT NOT NULL DEFAULT '',
                    last_read_start REAL,
                    last_read_end REAL,
                    last_read_count INTEGER,
                    last_read_partial INTEGER NOT NULL DEFAULT 0,
                    last_read_at REAL,
                    UNIQUE(platform,bot,group_id,reader_id,hour,minute,
                           schedule_kind,send_date)
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auto_summary_jobs)')}
            for name in ('read_start_minute', 'read_end_minute'):
                if name not in columns:
                    db.execute(f'ALTER TABLE auto_summary_jobs ADD COLUMN {name} INTEGER')
            if 'schedule_kind' not in columns:
                db.executescript('''
                    BEGIN IMMEDIATE;
                    CREATE TABLE auto_summary_jobs_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL, bot TEXT NOT NULL,
                        group_id TEXT NOT NULL, reader_id TEXT NOT NULL,
                        private_umo TEXT NOT NULL, hour INTEGER NOT NULL,
                        minute INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                        last_run_date TEXT NOT NULL DEFAULT '',
                        last_status TEXT NOT NULL DEFAULT '待运行',
                        last_sent_at REAL, created_at REAL NOT NULL,
                        initial_lower REAL NOT NULL,
                        cursor_id INTEGER NOT NULL DEFAULT 0,
                        read_start_minute INTEGER, read_end_minute INTEGER,
                        schedule_kind TEXT NOT NULL DEFAULT 'daily',
                        date_offset INTEGER NOT NULL DEFAULT 0,
                        read_date TEXT NOT NULL DEFAULT '',
                        send_date TEXT NOT NULL DEFAULT '',
                        UNIQUE(platform,bot,group_id,reader_id,hour,minute,
                               schedule_kind,send_date)
                    );
                    INSERT INTO auto_summary_jobs_new
                        (id,platform,bot,group_id,reader_id,private_umo,hour,minute,
                         enabled,last_run_date,last_status,last_sent_at,created_at,
                         initial_lower,cursor_id,read_start_minute,read_end_minute)
                    SELECT id,platform,bot,group_id,reader_id,private_umo,hour,minute,
                           enabled,last_run_date,last_status,last_sent_at,created_at,
                           initial_lower,cursor_id,read_start_minute,read_end_minute
                    FROM auto_summary_jobs;
                    DROP TABLE auto_summary_jobs;
                    ALTER TABLE auto_summary_jobs_new RENAME TO auto_summary_jobs;
                    COMMIT;
                ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auto_summary_jobs)')}
            if 'delivery_kind' not in columns:
                db.execute("ALTER TABLE auto_summary_jobs ADD COLUMN delivery_kind TEXT NOT NULL DEFAULT 'qq'")
            if 'email_to' not in columns:
                db.execute("ALTER TABLE auto_summary_jobs ADD COLUMN email_to TEXT NOT NULL DEFAULT ''")
            if 'read_end_inclusive' not in columns:
                db.execute('ALTER TABLE auto_summary_jobs ADD COLUMN read_end_inclusive INTEGER NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auto_summary_jobs)')}
            read_columns = {
                'last_read_start': 'REAL', 'last_read_end': 'REAL',
                'last_read_count': 'INTEGER',
                'last_read_partial': 'INTEGER NOT NULL DEFAULT 0',
                'last_read_at': 'REAL',
            }
            for name, definition in read_columns.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE auto_summary_jobs ADD COLUMN {name} {definition}')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def group_aliases(self, allowed):
        if not allowed:
            return {}
        with closing(self.connect()) as db:
            marks = ','.join('?' for _ in allowed)
            rows = db.execute(
                f'SELECT group_id, display_name FROM group_aliases WHERE group_id IN ({marks})',
                tuple(sorted(allowed))).fetchall()
            return {row['group_id']: row['display_name'] for row in rows}

    def set_group_alias(self, group_id, display_name):
        with closing(self.connect()) as db, db:
            if display_name:
                db.execute('''INSERT INTO group_aliases(group_id,display_name) VALUES (?,?)
                    ON CONFLICT(group_id) DO UPDATE SET display_name=excluded.display_name''',
                    (group_id, display_name))
            else:
                db.execute('DELETE FROM group_aliases WHERE group_id=?', (group_id,))

    def set_group_aliases(self, names):
        """Update supplied group names in one transaction; omitted groups are untouched."""
        with closing(self.connect()) as db, db:
            for group_id, display_name in names.items():
                if display_name:
                    db.execute('''INSERT INTO group_aliases(group_id,display_name) VALUES (?,?)
                        ON CONFLICT(group_id) DO UPDATE SET display_name=excluded.display_name''',
                        (group_id, display_name))
                else:
                    db.execute('DELETE FROM group_aliases WHERE group_id=?', (group_id,))

    def insert(self, message):
        with closing(self.connect()) as db, db:
            db.execute('''INSERT OR IGNORE INTO messages
                (platform,bot,group_id,message_id,timestamp,sender_id,sender_name,content,truncated)
                VALUES (:platform,:bot,:group_id,:message_id,:timestamp,:sender_id,:sender_name,:content,:truncated)
            ''', message)
            if message.get('media_payload'):
                db.execute('INSERT OR IGNORE INTO media_payloads VALUES (?,?,?,?,?)',
                           (message['platform'], message['bot'], message['group_id'],
                            message['message_id'], message['media_payload']))

    def insert_history(self, messages):
        """Merge one fetched history window in a single transaction."""
        added = 0
        with closing(self.connect()) as db, db:
            for message in messages:
                result = db.execute('''INSERT OR IGNORE INTO messages
                    (platform,bot,group_id,message_id,timestamp,sender_id,
                     sender_name,content,truncated)
                    VALUES (:platform,:bot,:group_id,:message_id,:timestamp,
                            :sender_id,:sender_name,:content,:truncated)''', message)
                added += result.rowcount
                if message.get('media_payload'):
                    db.execute('INSERT OR IGNORE INTO media_payloads VALUES (?,?,?,?,?)',
                               (message['platform'], message['bot'], message['group_id'],
                                message['message_id'], message['media_payload']))
        return added

    def purge(self, before):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM messages WHERE timestamp < ?', (before,))
            for table in ('media_payloads', 'media_cache', 'auto_image_cache'):
                db.execute(f'''DELETE FROM {table} WHERE NOT EXISTS
                    (SELECT 1 FROM messages m WHERE m.platform={table}.platform
                     AND m.bot={table}.bot AND m.group_id={table}.group_id
                     AND m.message_id={table}.message_id)''')

    def media_data(self, scope, provider):
        with closing(self.connect()) as db:
            payload = db.execute('SELECT payload FROM media_payloads WHERE platform=? AND bot=? AND group_id=? AND message_id=?', scope).fetchone()
            cached = db.execute('SELECT result FROM media_cache WHERE platform=? AND bot=? AND group_id=? AND message_id=? AND provider=?', (*scope, provider)).fetchone()
            return (json.loads(payload[0]) if payload else None,
                    json.loads(cached[0]) if cached else None)

    def cached_media_results(self, platform, bot, group, provider, message_ids):
        ids = list(dict.fromkeys(str(value) for value in message_ids if str(value)))
        result = {}
        with closing(self.connect()) as db:
            for offset in range(0, len(ids), 300):
                batch = ids[offset:offset + 300]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute('''SELECT message_id,result FROM media_cache
                    WHERE platform=? AND bot=? AND group_id=? AND provider=?
                    AND message_id IN (''' + placeholders + ')',
                    (platform, bot, group, provider, *batch)).fetchall()
                result.update((row['message_id'], json.loads(row['result'])) for row in rows)
        return result

    def cached_auto_images(self, platform, bot, group, provider, message_ids):
        ids = list(dict.fromkeys(str(value) for value in message_ids if str(value)))
        result = {}
        with closing(self.connect()) as db:
            for offset in range(0, len(ids), 300):
                batch = ids[offset:offset + 300]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute('''SELECT message_id,image_index,result FROM auto_image_cache
                    WHERE platform=? AND bot=? AND group_id=? AND provider=?
                    AND message_id IN (''' + placeholders + ')',
                    (platform, bot, group, provider, *batch)).fetchall()
                result.update(((row['message_id'], row['image_index']), row['result'])
                              for row in rows)
        return result

    def save_auto_image_caches(self, platform, bot, group, provider, entries):
        if not entries:
            return
        with closing(self.connect()) as db, db:
            db.executemany('''INSERT OR REPLACE INTO auto_image_cache
                (platform,bot,group_id,message_id,image_index,provider,result)
                VALUES (?,?,?,?,?,?,?)''',
                ((platform, bot, group, mid, image_index, provider, content)
                 for mid, image_index, content in entries))

    def save_media_cache(self, scope, provider, result):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO media_cache VALUES (?,?,?,?,?,?)',
                       (*scope, provider, json.dumps(result, ensure_ascii=False)))

    def groups(self, platform, bot, allowed, before):
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT group_id, COUNT(*) AS message_count,
                MIN(timestamp) AS first_time, MAX(timestamp) AS last_time
                FROM messages WHERE platform=? AND bot=? AND timestamp>=?
                GROUP BY group_id ORDER BY last_time DESC''', (platform, bot, before))
            return [dict(r) for r in rows if r['group_id'] in allowed]

    def sources(self, allowed, before):
        with closing(self.connect()) as db:
            rows = db.execute(
                '''SELECT platform, bot, COUNT(*) AS message_count,
                   COUNT(DISTINCT group_id) AS group_count,
                   MIN(timestamp) AS first_time, MAX(timestamp) AS last_time
                   FROM messages WHERE timestamp>=?
                   GROUP BY platform, bot ORDER BY last_time DESC''',
                (before,),
            ).fetchall()
            result = []
            for row in rows:
                scope = dict(row)
                count = db.execute(
                    '''SELECT COUNT(*) FROM messages
                       WHERE platform=? AND bot=? AND timestamp>=? AND group_id IN ({})'''.format(
                           ','.join('?' for _ in allowed) or "''"
                       ),
                    (scope['platform'], scope['bot'], before, *sorted(allowed)),
                ).fetchone()[0]
                if count:
                    scope['message_count'] = count
                    result.append(scope)
            return result

    def read(self, platform, bot, group, start, end, keyword, sender,
             after_time=-1, after_id=0, snapshot=None, limit=101):
        with closing(self.connect()) as db:
            if snapshot is None:
                snapshot = db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
            where = '''m.platform=? AND m.bot=? AND m.group_id=? AND m.timestamp>=? AND m.timestamp<?
                AND m.id<=? AND (?='' OR instr(m.content,?)>0) AND (?='' OR m.sender_id=?)'''
            args = (platform, bot, group, start, end, snapshot, keyword, keyword, sender, sender)
            total = db.execute('SELECT COUNT(*) FROM messages m WHERE ' + where, args).fetchone()[0]
            rows = db.execute('''SELECT m.*, p.payload AS media_payload FROM messages m
                LEFT JOIN media_payloads p ON p.platform=m.platform AND p.bot=m.bot
                    AND p.group_id=m.group_id AND p.message_id=m.message_id
                WHERE ''' + where + '''
                AND (m.timestamp>? OR (m.timestamp=? AND m.id>?)) ORDER BY m.timestamp,m.id LIMIT ?''',
                args + (after_time, after_time, after_id, limit)).fetchall()
            return total, snapshot, [dict(row) for row in rows]

    def resolve_messages(self, platform, bot, group, message_ids):
        ids = list(dict.fromkeys(str(value) for value in message_ids if str(value)))
        if not ids:
            return {}
        result = {}
        with closing(self.connect()) as db:
            for offset in range(0, len(ids), 300):
                batch = ids[offset:offset + 300]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute(
                    '''SELECT message_id,timestamp,sender_id,sender_name,content,truncated
                       FROM messages WHERE platform=? AND bot=? AND group_id=?
                       AND message_id IN (''' + placeholders + ')',
                    (platform, bot, group, *batch),
                ).fetchall()
                result.update((row['message_id'], dict(row)) for row in rows)
        return result

    def get_checkpoint(self, platform, bot, group, reader_id):
        with closing(self.connect()) as db:
            row = db.execute(
                '''SELECT checkpoint FROM summary_checkpoints
                   WHERE platform=? AND bot=? AND group_id=? AND reader_id=?''',
                (platform, bot, group, reader_id),
            ).fetchone()
            return row['checkpoint'] if row else None

    def set_checkpoint(self, platform, bot, group, reader_id, checkpoint):
        with closing(self.connect()) as db, db:
            db.execute(
                '''INSERT INTO summary_checkpoints(platform,bot,group_id,reader_id,checkpoint)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(platform,bot,group_id,reader_id)
                   DO UPDATE SET checkpoint=excluded.checkpoint''',
                (platform, bot, group, reader_id, checkpoint),
            )

    def add_auto_job(self, platform, bot, group, reader, private_umo, hour, minute,
                     initial_lower, created_at, read_start_minute=None,
                     read_end_minute=None, schedule_kind='daily', date_offset=0,
                     read_date='', send_date='', delivery_kind='qq', email_to='',
                     read_end_inclusive=False):
        with closing(self.connect()) as db, db:
            db.execute('''INSERT INTO auto_summary_jobs
                (platform,bot,group_id,reader_id,private_umo,hour,minute,initial_lower,
                 created_at,read_start_minute,read_end_minute,schedule_kind,
                 date_offset,read_date,send_date,delivery_kind,email_to,
                 read_end_inclusive)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,bot,group_id,reader_id,hour,minute,
                            schedule_kind,send_date)
                DO UPDATE SET private_umo=excluded.private_umo,enabled=1,
                read_start_minute=COALESCE(excluded.read_start_minute,
                                            auto_summary_jobs.read_start_minute),
                read_end_minute=COALESCE(excluded.read_end_minute,
                                          auto_summary_jobs.read_end_minute),
                read_end_inclusive=excluded.read_end_inclusive,
                date_offset=CASE WHEN excluded.read_start_minute IS NULL
                    THEN auto_summary_jobs.date_offset ELSE excluded.date_offset END,
                read_date=excluded.read_date,
                last_run_date=CASE WHEN auto_summary_jobs.delivery_kind<>excluded.delivery_kind
                    OR auto_summary_jobs.email_to<>excluded.email_to
                    THEN '' ELSE auto_summary_jobs.last_run_date END,
                last_status=CASE WHEN auto_summary_jobs.delivery_kind<>excluded.delivery_kind
                    OR auto_summary_jobs.email_to<>excluded.email_to
                    THEN '待运行' ELSE auto_summary_jobs.last_status END,
                delivery_kind=excluded.delivery_kind,email_to=excluded.email_to''',
                (platform, bot, group, reader, private_umo, hour, minute,
                 initial_lower, created_at, read_start_minute, read_end_minute,
                 schedule_kind, date_offset, read_date, send_date,
                 delivery_kind, email_to, int(read_end_inclusive)))
            row = db.execute('''SELECT id FROM auto_summary_jobs
                WHERE platform=? AND bot=? AND group_id=? AND reader_id=?
                AND hour=? AND minute=? AND schedule_kind=? AND send_date=?''',
                (platform, bot, group, reader, hour, minute,
                 schedule_kind, send_date)).fetchone()
            return row['id']

    def update_auto_job_admin(self, job_id, platform, bot, group, reader, private_umo,
                              hour, minute, initial_lower, changed_at,
                              read_start_minute, read_end_minute,
                              schedule_kind='daily', date_offset=0,
                              read_date='', send_date='', delivery_kind='qq',
                              email_to='', read_end_inclusive=False):
        """Update one job by ID; a new group or reader starts a new read cursor."""
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM auto_summary_jobs WHERE id=? AND enabled=1',
                             (job_id,)).fetchone()
            if row is None:
                return False
            identity_changed = (row['platform'], row['bot'], row['group_id'],
                                row['reader_id']) != (platform, bot, group, reader)
            schedule_changed = (identity_changed or
                                (row['hour'], row['minute'], row['schedule_kind'],
                                 row['date_offset'], row['read_date'], row['send_date'],
                                 row['read_start_minute'], row['read_end_minute'],
                                 row['read_end_inclusive'],
                                 row['delivery_kind'], row['email_to']) !=
                                (hour, minute, schedule_kind, date_offset, read_date,
                                 send_date, read_start_minute, read_end_minute,
                                 int(read_end_inclusive),
                                 delivery_kind, email_to))
            db.execute('''UPDATE auto_summary_jobs SET
                platform=?,bot=?,group_id=?,reader_id=?,private_umo=?,hour=?,minute=?,
                read_start_minute=?,read_end_minute=?,read_end_inclusive=?,schedule_kind=?,date_offset=?,
                read_date=?,send_date=?,delivery_kind=?,email_to=?,
                initial_lower=CASE WHEN ? THEN ? ELSE initial_lower END,
                cursor_id=CASE WHEN ? THEN 0 ELSE cursor_id END,
                last_sent_at=CASE WHEN ? THEN NULL ELSE last_sent_at END,
                created_at=CASE WHEN ? THEN ? ELSE created_at END,
                last_run_date=CASE WHEN ? THEN '' ELSE last_run_date END,
                last_status=CASE WHEN ? THEN '待运行' ELSE last_status END
                WHERE id=?''',
                (platform, bot, group, reader, private_umo, hour, minute,
                 read_start_minute, read_end_minute, int(read_end_inclusive), schedule_kind, date_offset,
                 read_date, send_date, delivery_kind, email_to,
                 identity_changed, initial_lower, identity_changed,
                 identity_changed, schedule_changed, changed_at,
                 schedule_changed, schedule_changed, job_id))
            return True

    def list_auto_jobs(self, platform, bot, reader):
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT id,group_id,hour,minute,enabled,last_run_date,
                last_status,last_sent_at,read_start_minute,read_end_minute,
                read_end_inclusive,
                schedule_kind,date_offset,read_date,send_date,delivery_kind,email_to,
                last_read_start,last_read_end,last_read_count,last_read_partial,last_read_at
                FROM auto_summary_jobs
                WHERE platform=? AND bot=? AND reader_id=? AND enabled=1
                ORDER BY hour,minute,id''', (platform, bot, reader)).fetchall()
            return [dict(row) for row in rows]

    def delete_auto_job(self, platform, bot, reader, job_id):
        with closing(self.connect()) as db, db:
            result = db.execute('''DELETE FROM auto_summary_jobs
                WHERE id=? AND platform=? AND bot=? AND reader_id=?''',
                (job_id, platform, bot, reader))
            return result.rowcount > 0

    def delete_auto_jobs_for_group(self, platform, bot, reader, group):
        with closing(self.connect()) as db, db:
            result = db.execute('''DELETE FROM auto_summary_jobs
                WHERE platform=? AND bot=? AND reader_id=? AND group_id=?''',
                (platform, bot, reader, group))
            return result.rowcount

    def delete_auto_job_admin(self, job_id):
        with closing(self.connect()) as db, db:
            result = db.execute('DELETE FROM auto_summary_jobs WHERE id=?', (job_id,))
            return result.rowcount > 0

    def group_sources(self, groups, before):
        if not groups:
            return []
        placeholders = ','.join('?' for _ in groups)
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT DISTINCT platform,bot,group_id FROM messages
                WHERE group_id IN (''' + placeholders + ''') AND timestamp>=?
                ORDER BY group_id,platform,bot''', (*sorted(groups), before)).fetchall()
            return [dict(row) for row in rows]

    def known_private_umo(self, platform, bot, reader):
        with closing(self.connect()) as db:
            row = db.execute('''SELECT private_umo FROM auto_summary_jobs
                WHERE platform=? AND bot=? AND reader_id=?
                ORDER BY id DESC LIMIT 1''', (platform, bot, reader)).fetchone()
            return row['private_umo'] if row else None

    def all_auto_jobs(self):
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT * FROM auto_summary_jobs
                WHERE enabled=1 ORDER BY hour,minute,id''').fetchall()
            return [dict(row) for row in rows]

    def auto_job_active(self, job_id, expected=None):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM auto_summary_jobs WHERE id=?',
                             (job_id,)).fetchone()
            if not row or not row['enabled']:
                return False
            if expected is None:
                return True
            fields = ('platform', 'bot', 'group_id', 'reader_id', 'private_umo',
                      'hour', 'minute', 'read_start_minute', 'read_end_minute',
                      'read_end_inclusive',
                      'schedule_kind', 'date_offset', 'read_date', 'send_date',
                      'delivery_kind', 'email_to',
                      'created_at')
            return all(row[field] == expected[field] for field in fields)

    def claim_auto_job(self, job_id, date):
        with closing(self.connect()) as db, db:
            result = db.execute('''UPDATE auto_summary_jobs
                SET last_run_date=?,last_status='执行中：读取记录'
                WHERE id=? AND enabled=1 AND last_run_date<>?''',
                (date, job_id, date))
            return result.rowcount > 0

    def reopen_missed_auto_job(self, job_id, date):
        """Reopen only a previously skipped run that is still in its catch-up window."""
        with closing(self.connect()) as db, db:
            result = db.execute('''UPDATE auto_summary_jobs
                SET last_run_date='',last_status='待运行'
                WHERE id=? AND enabled=1 AND last_run_date=?
                AND last_status IN ('错过计划时间，未推送',
                                    'QQ接入未恢复，未推送')''', (job_id, date))
            return result.rowcount > 0

    def set_auto_job_status(self, job_id, status):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE auto_summary_jobs SET last_status=? WHERE id=? AND enabled=1',
                       (status, job_id))

    def set_auto_job_read_stats(self, job_id, start, end, count, partial, read_at):
        with closing(self.connect()) as db, db:
            db.execute('''UPDATE auto_summary_jobs SET last_read_start=?,last_read_end=?,
                last_read_count=?,last_read_partial=?,last_read_at=?
                WHERE id=? AND enabled=1''',
                (start, end, count, int(partial), read_at, job_id))

    def release_auto_job(self, job_id, status):
        """Allow a safe retry when the QQ API was unavailable before sending."""
        with closing(self.connect()) as db, db:
            db.execute('''UPDATE auto_summary_jobs
                SET last_run_date='',last_status=? WHERE id=? AND enabled=1''',
                (status, job_id))

    def auto_messages(self, job, retained_from, limit=None, window_end=None,
                      window_start=None):
        """Read one exact daily window, or use the legacy incremental cursor."""
        with closing(self.connect()) as db:
            snapshot = db.execute('''SELECT COALESCE(MAX(id),0) FROM messages
                WHERE platform=? AND bot=? AND group_id=?''',
                (job['platform'], job['bot'], job['group_id'])).fetchone()[0]
            if window_start is not None and window_end is not None:
                rows = db.execute('''SELECT m.id,m.message_id,m.timestamp,m.sender_name,
                    m.content,m.truncated,p.payload AS media_payload
                    FROM messages m LEFT JOIN media_payloads p
                    ON p.platform=m.platform AND p.bot=m.bot
                    AND p.group_id=m.group_id AND p.message_id=m.message_id
                    WHERE m.platform=? AND m.bot=? AND m.group_id=?
                    AND m.id<=? AND m.timestamp>=? AND m.timestamp<?
                    ORDER BY m.timestamp,m.id''' +
                    ('' if limit is None else ' LIMIT ?'),
                    (job['platform'], job['bot'], job['group_id'], snapshot,
                     max(retained_from, window_start), window_end) +
                    (() if limit is None else (limit + 1,))).fetchall()
                return ([dict(row) for row in rows] if limit is None else
                        [dict(row) for row in rows[:limit]]), (limit is not None and len(rows) > limit)
            lower = max(retained_from, job['initial_lower'] if not job['cursor_id']
                        else retained_from)
            upper = window_end if window_end is not None else 4102444800
            start_minute = job.get('read_start_minute')
            end_minute = job.get('read_end_minute')
            range_sql = ''
            range_args = ()
            if start_minute is not None and end_minute is not None:
                # Beijing time is UTC+08:00. Apply the daily interval across all
                # unprocessed days so a >limit backlog remains eligible tomorrow.
                minute_sql = 'CAST(((m.timestamp + 28800) % 86400) / 60 AS INTEGER)'
                exclusive_end = end_minute + int(bool(job.get('read_end_inclusive')))
                operator = 'AND' if start_minute < exclusive_end else 'OR'
                range_sql = (f' AND ({minute_sql}>=? {operator} '
                             f'{minute_sql}<?)')
                range_args = (start_minute, exclusive_end)
            rows = db.execute('''SELECT m.id,m.message_id,m.timestamp,m.sender_name,
                m.content,m.truncated,p.payload AS media_payload
                FROM messages m LEFT JOIN media_payloads p
                ON p.platform=m.platform AND p.bot=m.bot
                AND p.group_id=m.group_id AND p.message_id=m.message_id
                WHERE m.platform=? AND m.bot=? AND m.group_id=?
                AND m.id>? AND m.id<=? AND m.timestamp>=? AND m.timestamp<?
                ''' + range_sql + ''' ORDER BY m.id''' +
                ('' if limit is None else ' LIMIT ?'),
                (job['platform'], job['bot'], job['group_id'], job['cursor_id'],
                 snapshot, lower, upper, *range_args) +
                (() if limit is None else (limit + 1,))).fetchall()
            return ([dict(row) for row in rows] if limit is None else
                    [dict(row) for row in rows[:limit]]), (limit is not None and len(rows) > limit)

    def finish_auto_job(self, job_id, status, cursor_id=None, sent_at=None):
        with closing(self.connect()) as db, db:
            if cursor_id is None:
                db.execute('UPDATE auto_summary_jobs SET last_status=? WHERE id=?',
                           (status, job_id))
            else:
                db.execute('''UPDATE auto_summary_jobs
                    SET last_status=?,cursor_id=?,last_sent_at=? WHERE id=?''',
                    (status, cursor_id, sent_at, job_id))
