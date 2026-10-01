"""Storage boundary. SQLite operations stay here, never in strategy or scheduling."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class SignalRepository(Protocol):
    def get_setting(self, key: str, default=None): ...
    def set_setting(self, key: str, value): ...
    def checkpoint(self, key: str) -> dict | None: ...
    def save_checkpoint(self, key: str, value: dict): ...
    def turnover(self, key: str, day: str) -> float | None: ...
    def save_turnover(self, key: str, day: str, value: float): ...
    def insert_signal(self, value: dict) -> bool: ...
    def claim_notification(self, signal_id: str) -> bool: ...
    def invalidate(self, key: str, candle_time: str, updated_at: str): ...
    def signals(self, limit=100, offset=0, **filters) -> dict: ...
    def save_run(self, value: dict): ...
    def runs(self, limit=20, run_type=None, exclude_skipped=False) -> list[dict]: ...
    def close(self): ...


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class SQLiteSignalRepository:
    def __init__(self, path: str | Path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        with self.transaction() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_turnover (
                    key TEXT NOT NULL, day TEXT NOT NULL, turnover REAL NOT NULL,
                    PRIMARY KEY(key, day));
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, stream_key TEXT NOT NULL,
                    exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
                    period TEXT NOT NULL, signal_type TEXT NOT NULL, stage TEXT NOT NULL,
                    score INTEGER NOT NULL, candle_time TEXT NOT NULL, detected_at TEXT NOT NULL,
                    sar_flip_bars_ago INTEGER, ha_flip_bars_ago INTEGER, decline_bars INTEGER,
                    decline_pct REAL, rebound_pct REAL, previous_day_turnover REAL,
                    reasons TEXT NOT NULL, warnings TEXT NOT NULL, status TEXT NOT NULL,
                    notified INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS signals_detected ON signals(detected_at DESC, id);
                CREATE INDEX IF NOT EXISTS signals_stream ON signals(stream_key, status);
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, period TEXT NOT NULL,
                    processed INTEGER NOT NULL, matched INTEGER NOT NULL, failed INTEGER NOT NULL,
                    skipped INTEGER NOT NULL, warnings TEXT NOT NULL, duration_seconds REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_started ON scan_runs(started_at DESC);
            ''')
            # Interrupted runs are visible after a process restart, never left "running".
            rows = db.execute("SELECT payload FROM scan_runs WHERE status='running'").fetchall()
            for row in rows:
                value = json.loads(row['payload'])
                now = datetime.now(timezone.utc)
                value.update(status='failed', finished_at=now.isoformat(),
                             duration=round(max(0,(now-datetime.fromisoformat(value['started_at'])).total_seconds()),2))
                value.setdefault('warnings', []).append('interrupted_by_restart')
                self.save_run(value)

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, encoded(value)))

    def checkpoint(self, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def save_checkpoint(self, key, value):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?)', (key, encoded(value)))

    def turnover(self, key, day):
        with self.lock:
            row = self.db.execute('SELECT turnover FROM daily_turnover WHERE key=? AND day=?', (key, day)).fetchone()
        return row['turnover'] if row else None

    def save_turnover(self, key, day, value):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO daily_turnover VALUES (?,?,?)', (key, day, value))
            # Only the latest two UTC dates are useful for incremental filtering.
            db.execute('DELETE FROM daily_turnover WHERE day < date(?, \'-1 day\')', (day,))

    def insert_signal(self, value):
        columns = ('id','stream_key','exchange','market','pair','period','signal_type','stage','score',
                   'candle_time','detected_at','sar_flip_bars_ago','ha_flip_bars_ago','decline_bars',
                   'decline_pct','rebound_pct','previous_day_turnover','reasons','warnings','status',
                   'notified','created_at','updated_at')
        values = [encoded(value[k]) if k in ('reasons','warnings') else value.get(k) for k in columns]
        with self.transaction() as db:
            cursor = db.execute(f'INSERT OR IGNORE INTO signals ({",".join(columns)},payload) '
                                f'VALUES ({",".join("?" for _ in range(len(columns)+1))})', values+[encoded(value)])
            return cursor.rowcount == 1

    def claim_notification(self, signal_id):
        # Claim BEFORE sending. A lost webhook acknowledgement must not cause hourly redelivery.
        with self.transaction() as db:
            return db.execute('UPDATE signals SET notified=1 WHERE id=? AND notified=0', (signal_id,)).rowcount == 1

    def invalidate(self, key, candle_time, updated_at):
        with self.transaction() as db:
            db.execute("UPDATE signals SET status='invalid', updated_at=? "
                       "WHERE stream_key=? AND candle_time<? AND status='active'", (updated_at,key,candle_time))

    def signals(self, limit=100, offset=0, **filters):
        terms, args = [], []
        for name in ('exchange','market','period','stage'):
            value = filters.get(name)
            if value:
                terms.append("status='invalid'" if name == 'stage' and value == '失效' else f'{name}=?')
                if not (name == 'stage' and value == '失效'):
                    args.append(value)
                if name == 'stage' and value != '失效':
                    terms.append("status='active'")
        if filters.get('pair'):
            terms.append('instr(lower(pair),lower(?))>0')
            args.append(filters['pair'])
        for name, operator in (('date_from','>='),('date_to','<=')):
            if filters.get(name):
                terms.append(f'substr(detected_at,1,10){operator}?')
                args.append(filters[name])
        where = ' WHERE '+' AND '.join(terms) if terms else ''
        with self.lock:
            total = self.db.execute('SELECT COUNT(*) FROM signals'+where, args).fetchone()[0]
            rows = self.db.execute('SELECT payload,status,notified,updated_at FROM signals'+where+
                                   ' ORDER BY detected_at DESC,id LIMIT ? OFFSET ?', args+[limit,offset]).fetchall()
        items = [{**json.loads(row['payload']), 'status':row['status'], 'notified':bool(row['notified']),
                  'updated_at':row['updated_at']} for row in rows]
        return {'items':items,'total':total,'limit':limit,'offset':offset}

    def save_run(self, value):
        stats = value.get('stats', {})
        columns = ('id','type','started_at','finished_at','status','period','processed','matched','failed',
                   'skipped','warnings','duration_seconds','payload')
        fields = [value['id'],value.get('type','manual'),value['started_at'],value.get('finished_at'),
                  value['status'],','.join(value.get('options',{}).get('periods',['4h'])),
                  stats.get('processed_symbols',0),stats.get('matched_symbols',0),
                  stats.get('failed_symbols',0),stats.get('skipped_symbols',0),
                  encoded(value.get('warnings',[])),value.get('duration',0),encoded(value)]
        with self.transaction() as db:
            db.execute(f'INSERT OR REPLACE INTO scan_runs ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', fields)

    def runs(self, limit=20, run_type=None, exclude_skipped=False):
        terms, args = (['type=?'], [run_type]) if run_type else ([], [])
        if exclude_skipped:
            terms.append("status!='skipped'")
        where = ' WHERE '+' AND '.join(terms) if terms else ''
        with self.lock:
            rows = self.db.execute('SELECT payload FROM scan_runs'+where+' ORDER BY started_at DESC LIMIT ?', args+[limit]).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def close(self):
        with self.lock:
            self.db.close()
