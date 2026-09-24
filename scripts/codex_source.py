"""Version-tolerant, read-only source discovery. No business outcome logic here."""
import json
from pathlib import Path
import re
import sqlite3


def columns(conn, table):
    return {r['name'] for r in conn.execute('PRAGMA table_info(' + table + ')')}


def connect(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def config(path):
    result = {}
    # Whitelisted top-level scalars; never publish prompts or arbitrary new fields.
    for line in path.read_text().splitlines():
        match = re.fullmatch(r'(id|name|kind|status|rrule|created_at|updated_at|target_thread_id)\s*=\s*(.+)', line)
        if match:
            result[match[1]] = json.loads(match[2])
    if not {'id', 'name', 'status', 'rrule'} <= result.keys():
        raise ValueError('任务配置缺少必要字段')
    return result


class CodexSource:
    def __init__(self, home, cutoff):
        self.home, self.cutoff = Path(home), cutoff
        self.warnings, self.capabilities = [], {}
        self.configs = [config(p) for p in sorted((self.home / 'automations').glob('*/automation.toml'))]
        if not self.configs:
            raise RuntimeError('没有读到任务配置，保留上次有效数据')
        self.by_id = {c['id']: c for c in self.configs}
        state_paths = sorted(self.home.glob('state_*.sqlite'), key=lambda p: int(re.search(r'state_(\d+)', p.name)[1]), reverse=True)
        if not state_paths:
            raise RuntimeError('缺少会话索引')
        # Probe the newest schema; never fall back to a stale older state DB.
        self.threads = connect(state_paths[0])
        self.thread_cols = columns(self.threads, 'threads')
        if not {'id', 'rollout_path'} <= self.thread_cols:
            raise RuntimeError('会话索引格式不兼容')
        db_path = self.home / 'sqlite/codex-dev.db'
        self.db = connect(db_path) if db_path.exists() else None
        self.metadata = {}
        if self.db and 'id' in columns(self.db, 'automations'):
            cols = columns(self.db, 'automations') & {'id', 'last_run_at', 'next_run_at', 'target_thread_id'}
            self.metadata = {r['id']: dict(r) for r in self.db.execute('SELECT ' + ','.join(sorted(cols)) + ' FROM automations')}
            self.capabilities['schedule_metadata'] = {'last_run_at','next_run_at','target_thread_id'} <= cols
        else:
            self.capabilities['schedule_metadata'] = False
        if not self.capabilities['schedule_metadata']:
            self.warnings.append('调度元数据不可完整读取，部分上次／下次执行时间可能缺失。')

    def belongs(self, aid, created):
        cfg = self.by_id.get(aid)
        # IDs can be reused after deleting a task. Do not attribute an old task's
        # history to a newly created automation with the same ID.
        return bool(cfg and created / 1000 >= max(self.cutoff, cfg.get('created_at', 0) / 1000 - 120))

    def runs(self):
        rows, seen = [], set()
        cols = columns(self.db, 'automation_runs') if self.db else set()
        self.capabilities['run_index'] = {'thread_id','automation_id','created_at'} <= cols
        if self.capabilities['run_index']:
            chosen = cols & {'thread_id','automation_id','created_at','archived_assistant_message'}
            for row in self.db.execute('SELECT ' + ','.join(sorted(chosen)) + ' FROM automation_runs WHERE created_at>=?', (int(self.cutoff * 1000),)):
                r = dict(row)
                if self.belongs(r['automation_id'], r['created_at']):
                    rows.append(r); seen.add(r['thread_id'])
        else:
            self.warnings.append('运行索引不可用，已尝试从明确标记的会话恢复；覆盖可能不完整。')
        self.capabilities['session_headers'] = {'id','first_user_message','created_at','source'} <= self.thread_cols
        if self.capabilities['session_headers']:
            for row in self.threads.execute("SELECT id,first_user_message,created_at FROM threads WHERE created_at>=? AND source NOT LIKE '%subagent%' AND first_user_message LIKE 'Automation:%'", (int(self.cutoff),)):
                match = re.search(r'^Automation ID: ([\w-]+)\s*$', row['first_user_message'], re.M)
                if match and self.belongs(match[1], row['created_at'] * 1000) and row['id'] not in seen:
                    rows.append({'thread_id':row['id'], 'automation_id':match[1], 'created_at':row['created_at']*1000, 'recovered':True})
                    seen.add(row['id'])
        else:
            self.warnings.append('会话任务标识不可读取，无法补充运行索引外的记录。')
        if not self.capabilities['run_index'] and not self.capabilities['session_headers']:
            raise RuntimeError('所有运行发现来源均不可用')
        return rows

    def rollout_path(self, tid):
        row = self.threads.execute('SELECT rollout_path FROM threads WHERE id=?', (tid,)).fetchone()
        return Path(row['rollout_path']) if row else None

    def close(self):
        if self.db:
            self.db.close()
        self.threads.close()
