#!/usr/bin/env python3
"""Read-only Codex adapter. Python 3.9+, no external dependencies.

Only configured automations and explicitly associated root sessions are read.
Public files never contain prompts, raw tool output or raw transcripts.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo
from codex_source import CodexSource

UTC = dt.timezone.utc
TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]
VERSION = 1
PARSER_VERSION = 2


def iso(value):
    if not value:
        return None
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
    if value > 100000000000:
        value /= 1000
    return dt.datetime.fromtimestamp(value, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def epoch(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def clean(text, limit=1600):
    """Small, deliberately conservative public-text redactor."""
    text = re.sub(r'<oai-mem-citation>[\s\S]*?</oai-mem-citation>', '', text or '')
    text = re.sub(r'::[a-z-]+\{[^\n]*\}', '', text)
    text = re.sub(r'```[\s\S]*?```', '[命令或原始输出已省略]', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://[^\s<>）)]+', '[链接]', text)
    text = re.sub(r'(?:/Users/|/private/|/tmp/|/home/|/var/|~/)[^\s`<>）)\]，；。]+', '[本地路径]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b', '[地址]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[邮箱]', text)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,}|(?:ou|oc|om|cli)_[A-Za-z0-9_]+)\b', '[标识已隐藏]', text)
    text = re.sub(r'(?i)\b(?:authorization|password|passwd|api[_-]?key|access[_-]?token|secret|bearer)\s*[:= ]\s*[\w./+=-]{6,}', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)(?:token|密钥|密码)\s*[:：=]\s*[^\s,，；;]+', '[凭据已隐藏]', text)
    text = re.sub(r'\b[A-Za-z0-9_+/=-]{48,}\b', '[长标识已隐藏]', text)
    text = text.replace('刘峥', '[接收人]')
    text = text.replace('**', '').replace('`', '')
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def classify(text):
    """Do not treat 'task_complete' or inbox state as business success."""
    plain = clean(text, 20000)
    lines = [s.strip(' #-•') for s in plain.splitlines() if s.strip()]
    if re.search(r'(?:含|包含|有)\s*[1-9]\d*\s*条无效消息|[1-9]\d*\s*条(?:消息)?(?:无效|丢失)', plain):
        return 'partial', 'inferred', '结果报告存在无效或丢失消息，按部分成功处理。'
    # Explicit overall result beats historical failures described later.
    overall = [s for s in lines if re.search(r'本次状态|最终状态|总体状态|整体状态|状态(?:为|是|[:：])|本轮结果|正式.*状态', s)]
    lead = '\n'.join(lines[:2])
    candidates = overall + [lead]
    for s in candidates:
        if re.search(r'\bpartial\b|部分成功|部分完成', s, re.I):
            return 'partial', 'explicit', clean(s, 230)
        if re.search(r'\bfailed\b|\bfailure\b|\bblocked\b|失败|未完成|未能完成|未执行|阻断|受阻', s, re.I) and not re.search(r'失败\s*[:：]?\s*0|0\s*失败|失败.*(?:解决|恢复)', s):
            return 'failed', 'explicit', clean(s, 230)
        if re.search(r'\bsuccess\b|成功(?:完成|：|:)|(?:已完成|已执行|已发布|完成了)|^成功', s, re.I):
            # A completion statement with an unresolved caveat stays uncertain.
            if re.search(r'但.*(?:失败|未完成|阻塞)|尚未.*(?:推送|发布|完成)', s):
                return 'unknown', 'unknown', clean(s, 230)
            return 'success', 'explicit', clean(s, 230)
    if re.search(r'\bpartial\b|部分成功', plain, re.I):
        return 'partial', 'inferred', next((clean(s, 230) for s in lines if re.search(r'partial|部分成功', s, re.I)), '')
    if re.search(r'(?:已推送|推送成功)', plain) and re.search(r'(?:远端一致|远端一致性|ahead/behind|HEAD =)', plain) and not re.search(r'未推送|尚未发布|未完成', plain):
        return 'success', 'inferred', '结果同时报告已推送和远端一致性验收；按规则推断。'
    if re.search(r'(?:本次|本轮|任务|预检|执行).{0,25}(?:失败|阻断|受阻|终止)|无法完成|未能执行|未能完成', plain):
        return 'failed', 'inferred', next((clean(s, 230) for s in lines if re.search(r'失败|阻断|受阻|终止|无法完成|未能', s)), '')
    if re.search(r'(?:receipt|回执)\s*[:：]\s*(?:未生成|不存在)', plain, re.I) and re.search(r'(?:push|推送)\s*[:：]\s*未执行', plain, re.I):
        return 'failed', 'inferred', '回执未生成且推送未执行，任务没有完成。'
    return 'unknown', 'unknown', '最终回复没有足够明确的整体结果，需人工确认。'


def text_content(payload):
    return '\n'.join(p.get('text', '') for p in payload.get('content', []) if isinstance(p, dict) and p.get('type') in ('input_text', 'output_text', 'text'))


def parse_rollout(path):
    turns = []
    current = None
    errors = 0
    with path.open() as stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            p = obj.get('payload', {})
            kind = p.get('type')
            timestamp = obj.get('timestamp')
            if obj.get('type') == 'event_msg' and kind == 'task_started':
                current = {'turn_id': p.get('turn_id'), 'started_at': iso(timestamp), 'completed_at': None, 'message': '', 'heartbeat': None, 'aborted': False}
                turns.append(current)
            elif current and obj.get('type') == 'response_item' and p.get('role') == 'user':
                content = text_content(p)
                match = re.search(r'^<heartbeat>\s*<automation_id>([^<]+)</automation_id>', content)
                if match:
                    current['heartbeat'] = match[1]
            elif current and obj.get('type') == 'event_msg' and kind == 'task_complete':
                if p.get('turn_id') and p['turn_id'] != current['turn_id']:
                    continue
                current['completed_at'] = iso(timestamp)
                current['message'] = p.get('last_agent_message') or current['message']
            elif current and obj.get('type') == 'event_msg' and kind == 'turn_aborted':
                current['aborted'] = True
            elif current and obj.get('type') == 'event_msg' and kind == 'task_aborted':
                current['aborted'] = True
            elif current and obj.get('type') == 'response_item' and p.get('role') == 'assistant' and p.get('phase') == 'final':
                current['message'] = text_content(p)
    return turns, errors


def summarize_run(aid, tid, started, turns, source, archived='', now=None):
    last = turns[-1] if turns else {}
    message = last.get('message') or archived or ''
    outcome, confidence, reason = classify(message)
    if not last.get('completed_at') and not message:
        recent = now - epoch(last.get('started_at') or started) < 6 * 3600
        outcome = 'running' if last and recent and not last.get('aborted') else 'unknown'
        confidence = 'unknown'
        reason = '已开始，尚未发现完成回执。' if outcome == 'running' else '没有可用的完成回执，无法确认结果。'
    public = clean(message)
    problem_lines = [s.strip() for s in public.splitlines() if re.search(r'失败|错误|阻断|受阻|未完成|无效|缺失|DNS|超时|timeout|permission denied', s, re.I)]
    return {'id': digest([aid, tid, started])[:20], 'automation_id': aid,
            'thread_id': tid, 'started_at': started, 'completed_at': last.get('completed_at'),
            'outcome': outcome, 'confidence': confidence, 'basis': reason,
            'summary': clean(message, 220) or reason, 'detail': public or reason,
            'error': '\n'.join(problem_lines[:3])[:500] if outcome in ('failed', 'partial', 'unknown') else '',
            'source': source, 'turn_count': len(turns),
            'duration_seconds': max(0, int(epoch(last['completed_at']) - epoch(started))) if last.get('completed_at') else None}


def schedule_label(rule):
    parts = dict(x.split('=', 1) for x in rule.replace('RRULE:', '').split(';') if '=' in x)
    hour, minute = parts.get('BYHOUR', ''), parts.get('BYMINUTE', '0')
    clock = (' ' + hour.zfill(2) + ':' + minute.zfill(2)) if hour and ',' not in hour else ''
    if parts.get('FREQ') == 'DAILY' and parts.get('INTERVAL', '1') == '1':
        return '每天' + clock
    if parts.get('FREQ') == 'WEEKLY' and parts.get('INTERVAL', '1') == '1':
        days = {'MO': '一', 'TU': '二', 'WE': '三', 'TH': '四', 'FR': '五', 'SA': '六', 'SU': '日'}
        return '每周' + '、'.join(days.get(d, d) for d in parts.get('BYDAY', '').split(',')) + clock
    return '自定义周期'


def collect(home, output, state_dir, now=None, days=90):
    now = now or dt.datetime.now(UTC).timestamp()
    cutoff = now - days * 86400
    manifest_path = output / 'manifest.json'
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    old_runs = [r for shard in old.get('shards', []) for r in json.loads((output / shard['file']).read_text())]
    for run in old_runs:
        run['id'] = digest([run['automation_id'], run['thread_id'], run['started_at']])[:20]
    source = CodexSource(home, cutoff)
    configs, metadata, indexed = source.configs, source.metadata, source.runs()
    cache_path = state_dir / 'parsed.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    new_cache, warnings, parsed = {}, list(source.warnings), {}

    def load(tid):
        if tid in parsed:
            return parsed[tid]
        path = source.rollout_path(tid)
        if not path or not path.is_file():
            warnings.append('部分运行缺少原始会话文件，已标注待确认。')
            parsed[tid] = []
            return []
        stat = path.stat()
        signature = [str(path), stat.st_size, stat.st_mtime_ns, PARSER_VERSION]
        cached = cache.get(tid)
        if cached and cached['signature'] == signature:
            result, errors = cached['turns'], cached.get('errors', 0)
        else:
            result, errors = parse_rollout(path)
        # Cache is private, ignored by git. It avoids rereading large unchanged logs.
        new_cache[tid] = {'signature': signature, 'turns': result, 'errors': errors}
        if errors:
            warnings.append('部分会话包含不完整日志行；结果按可读取回执展示。')
        parsed[tid] = result
        return result

    runs = []
    for row in indexed:
        tid, aid = row['thread_id'], row['automation_id']
        turns = load(tid)
        runs.append(summarize_run(aid, tid, iso(row['created_at']), turns, 'session_header' if row.get('recovered') else 'run_index', row.get('archived_assistant_message'), now))
    for config in configs:
        aid = config['id']
        tid = metadata.get(aid, {}).get('target_thread_id') or config.get('target_thread_id')
        if tid:
            for turn in load(tid):
                if turn.get('heartbeat') == aid and source.belongs(aid, epoch(turn['started_at']) * 1000):
                    runs.append(summarize_run(aid, tid, turn['started_at'], [turn], 'heartbeat_marker', now=now))
    # When sources disappear, preserve previously observed records with an explicit
    # stale-evidence flag. Never silently erase history or relabel it as fresh.
    current_by_id = {r['id']: r for r in runs}
    for prior in old_runs:
        if not source.belongs(prior['automation_id'], epoch(prior['started_at']) * 1000):
            continue
        current = current_by_id.get(prior['id'])
        missing = not current or (current['outcome'] == 'unknown' and current['turn_count'] == 0)
        if missing:
            kept = dict(prior, evidence_stale=True)
            if current:
                runs.remove(current)
            runs.append(kept)
            warnings.append('部分历史来源当前不可读取，保留上次结果并标注来源未复核。')
    # The same scheduled root may be discovered by both inbox and header adapters.
    # Use stable source-independent identity, then deduplicate.
    deduped = {}
    for run in runs:
        stable = digest([run['automation_id'], run['thread_id'], run['started_at']])[:20]
        run['id'] = stable
        if stable not in deduped or not run.get('evidence_stale'):
            deduped[stable] = run
    runs = list(deduped.values())
    runs.sort(key=lambda r: r['started_at'], reverse=True)
    tasks = []
    for c in configs:
        aid, meta = c['id'], metadata.get(c['id'], {})
        latest = next((r for r in runs if r['automation_id'] == aid), None)
        last_run = iso(meta.get('last_run_at'))
        missing = bool(last_run and (not latest or epoch(last_run) - epoch(latest['started_at']) > 120))
        overdue = bool(c['status'] == 'ACTIVE' and meta.get('next_run_at') and now - epoch(iso(meta['next_run_at'])) > 7200)
        tasks.append({'id': aid, 'name': clean(c['name'], 100), 'status': c['status'], 'kind': c.get('kind', 'cron'),
                      'schedule': schedule_label(c['rrule']), 'last_run_at': last_run,
                      'next_run_at': iso(meta.get('next_run_at')), 'latest_run_id': latest['id'] if latest else None,
                      'coverage_gap': missing, 'possibly_overdue': overdue,
                      'run_count': sum(r['automation_id'] == aid for r in runs)})
    source.close()
    months = {}
    for run in runs:
        month = dt.datetime.fromisoformat(run['started_at'].replace('Z', '+00:00')).astimezone(TZ).strftime('%Y-%m')
        months.setdefault(month, []).append(run)
    content_hash = digest({'tasks': tasks, 'runs': runs, 'warnings': sorted(set(warnings)), 'capabilities': source.capabilities})
    changed = content_hash != old.get('content_hash')
    publish = changed or old.get('heartbeat_minutes') != 120 or old.get('stale_after_minutes') != 180 or now - epoch(old.get('collected_at')) >= 7200
    if publish:
        # Immutable, content-addressed shards prevent mixed-version browser reads.
        shards = []
        for month, records in sorted(months.items(), reverse=True):
            filename = 'runs-' + month + '-' + digest(records)[:12] + '.json'
            atomic_json(output / filename, records)
            shards.append({'file': filename, 'count': len(records)})
        manifest = {'schema_version': VERSION, 'collected_at': iso(now), 'content_hash': content_hash,
                    'timezone': 'Asia/Shanghai', 'retention_days': days, 'collection_interval_minutes': 30,
                    'heartbeat_minutes': 120, 'stale_after_minutes': 180, 'run_count': len(runs),
                    'window_start': iso(cutoff), 'coverage_start': runs[-1]['started_at'] if runs else None,
                    'warnings': sorted(set(warnings)), 'tasks': tasks, 'shards': shards,
                    'source_capabilities': source.capabilities,
                    'classification_note': '依据最终回复归类，不代表独立复核业务系统；不明确的结果标记待确认。'}
        atomic_json(manifest_path, manifest)
        # Keep previous manifest shards for cached readers during deployment.
        keep = {s['file'] for s in shards + old.get('shards', [])}
        for p in output.glob('runs-*.json'):
            if p.name not in keep:
                p.unlink()
    atomic_json(cache_path, new_cache)
    atomic_json(state_dir / 'collection-status.json', {'checked_at': iso(now), 'ok': True, 'changed': changed, 'publish': publish, 'run_count': len(runs)})
    return {'ok': True, 'changed': changed, 'publish': publish, 'tasks': len(tasks), 'runs': len(runs), 'outcomes': {key: sum(r['outcome'] == key for r in runs) for key in ('success','partial','failed','unknown','running')}, 'warnings': sorted(set(warnings))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/data')
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.local')
    args = parser.parse_args()
    try:
        result = collect(args.codex_home, args.output, args.state_dir)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Fail closed. Do not publish empty data or a false freshness heartbeat.
        atomic_json(args.state_dir / 'collection-status.json', {'checked_at': iso(dt.datetime.now(UTC).timestamp()), 'ok': False, 'error': type(exc).__name__})
        print('Collection failed; previous published data preserved: ' + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
