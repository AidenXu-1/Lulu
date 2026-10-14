"""Private media inventory. Job state remains in the existing Core ledger."""
import os
from pathlib import Path
import sqlite3
from threading import RLock

from .contracts import LiveError

def directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise LiveError('请选择实际文件夹，暂不支持符号链接')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise LiveError('保存位置不存在，请重新选择')
    return path

class LiveIndex:
    def __init__(self, root):
        self.root = directory(root, create=True)
        self.lock = RLock()
        path = self.root / 'index.sqlite3'
        if path.is_symlink() or any((self.root / (path.name + suffix)).is_symlink() for suffix in ('-wal', '-shm', '-journal')):
            raise LiveError('直播索引无法安全读取，原文件已保留')
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        if not path.is_file() or path.stat().st_uid != os.getuid():
            raise LiveError('直播索引所属用户无效')
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hosts(
                id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, sec_uid TEXT NOT NULL DEFAULT '',
                web_rid TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, checked REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'resolving');
            CREATE UNIQUE INDEX IF NOT EXISTS sec_identity ON hosts(sec_uid) WHERE sec_uid != '';
            CREATE UNIQUE INDEX IF NOT EXISTS room_identity ON hosts(web_rid) WHERE web_rid != '';
            CREATE TABLE IF NOT EXISTS recordings(
                id TEXT PRIMARY KEY, host_id TEXT NOT NULL, name TEXT NOT NULL, job_id TEXT NOT NULL,
                directory TEXT NOT NULL UNIQUE, started REAL NOT NULL, ended REAL,
                reason TEXT NOT NULL DEFAULT '', gaps TEXT NOT NULL DEFAULT '[]');
            CREATE INDEX IF NOT EXISTS recording_order ON recordings(started DESC);
            CREATE TABLE IF NOT EXISTS segments(
                id TEXT PRIMARY KEY, recording_id TEXT NOT NULL REFERENCES recordings(id),
                path TEXT NOT NULL UNIQUE, started REAL NOT NULL, duration REAL NOT NULL,
                bytes INTEGER NOT NULL, closed INTEGER NOT NULL, valid INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS segment_recording ON segments(recording_id,started);
        ''')
        # Older status values mix recorder phases with platform observations.
        # Keep them intact; only a fresh response can populate the new projection.
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(hosts)')}
        for name, default in (('live_state', 'unknown'), ('live_error', '')):
            if name not in columns:
                self.db.execute(f"ALTER TABLE hosts ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        self.db.commit()

    def all(self, sql, values=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, values).fetchall()]

    def one(self, sql, values=()):
        rows = self.all(sql, values)
        return rows[0] if rows else None

    def write(self, sql, values=()):
        with self.lock, self.db:
            return self.db.execute(sql, values).rowcount

    def setting(self, key, default=''):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row['value'] if row else default

    def forget_recording(self, rid):
        # Media is already in the system Trash. Remove both projections together.
        with self.lock, self.db:
            self.db.execute('DELETE FROM segments WHERE recording_id=?', (rid,))
            self.db.execute('DELETE FROM recordings WHERE id=?', (rid,))

    def set_setting(self, key, value):
        self.write('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def close(self):
        with self.lock:
            self.db.close()
