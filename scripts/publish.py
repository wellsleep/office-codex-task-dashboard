#!/usr/bin/env python3
"""Collect, validate and publish only docs/data, with locking and retryable state."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import hashlib
import shutil

from collect import ROOT, atomic_json, collect, iso


def git(*args, check=True):
    result = subprocess.run(['git', *args], cwd=ROOT, text=True, capture_output=True, timeout=120,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT':'0'})
    if check and result.returncode:
        raise RuntimeError('git ' + args[0] + ' failed: ' + result.stderr.strip()[:300])
    return result.stdout.rstrip('\n')


def validate_public(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    names, ids = set(), set()
    for task in manifest['tasks']:
        if task['id'] in names:
            raise ValueError('duplicate task')
        names.add(task['id'])
    count = 0
    for shard in manifest['shards']:
        if not re.fullmatch(r'runs-\d{4}-\d{2}-[a-f0-9]{12}\.json', shard['file']):
            raise ValueError('unexpected shard path')
        rows = json.loads((folder / shard['file']).read_text())
        if len(rows) != shard['count']:
            raise ValueError('shard count mismatch')
        for row in rows:
            if row['automation_id'] not in names or row['id'] in ids:
                raise ValueError('broken run identity')
            ids.add(row['id'])
            if row['outcome'] not in ('success','partial','failed','unknown','running'):
                raise ValueError('invalid outcome')
        count += len(rows)
    if count != manifest['run_count']:
        raise ValueError('total count mismatch')
    for task in manifest['tasks']:
        if task['latest_run_id'] and task['latest_run_id'] not in ids:
            raise ValueError('missing latest run')
    forbidden = re.compile(r'-----BEGIN .*PRIVATE KEY|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{20,}|/Users/|/private/|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:ou|om|oc)_[A-Za-z0-9_]{10,}')
    for p in folder.glob('*.json'):
        if forbidden.search(p.read_text()):
            raise ValueError('public-data redaction check failed: ' + p.name)


def sync_remote():
    # Never reset, force push, auto-merge, or stage user changes.
    if git('status', '--porcelain'):
        raise RuntimeError('working tree is not clean; publisher will not stage existing changes')
    git('fetch', 'origin', 'main')
    ahead, behind = map(int, git('rev-list','--left-right','--count','HEAD...origin/main').split())
    if ahead and behind:
        raise RuntimeError('local and remote have diverged; manual review needed')
    if behind:
        git('merge','--ff-only','origin/main')
        # New executable code must take effect in a fresh invocation.
        return False
    if ahead:
        # A previous push may have failed; allow only our own data-only commits.
        for commit in git('rev-list','origin/main..HEAD').splitlines():
            if not git('show','-s','--format=%s',commit).startswith('data: refresh automation snapshot '):
                raise RuntimeError('unpublished non-data commit; manual push needed')
            changed = git('diff-tree','--no-commit-id','--name-only','-r',commit).splitlines()
            if any(not name.startswith('docs/data/') for name in changed):
                raise RuntimeError('pending commit changes more than public data')
        git('push','origin','HEAD:main')
    return True


def file_hashes(folder):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.json')}


def recover_prepared(local):
    journal = local / 'prepared.json'
    if not journal.exists():
        return
    plan = json.loads(journal.read_text())
    dirty = git('status','--porcelain','--untracked-files=all').splitlines()
    if not dirty:
        journal.unlink(); return
    if git('rev-parse','HEAD') != plan['base'] or any(not line[3:].startswith('docs/data/') for line in dirty):
        raise RuntimeError('prepared publication conflicts with local changes')
    if file_hashes(ROOT / 'docs/data') != plan['files']:
        raise RuntimeError('prepared public files were modified; manual review needed')
    validate_public(ROOT / 'docs/data')
    git('add','-A','--','docs/data')
    git('diff','--cached','--check')
    git('commit','-m',plan['message'])
    journal.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    local.chmod(0o700)
    with (local / 'publish.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another publisher is active; skipped.'); return 0
        try:
            if args.check_only:
                validate_public(ROOT / 'docs/data')
                print('Public schema, counts, references and redaction checks passed.'); return 0
            if git('branch','--show-current') != 'main':
                raise RuntimeError('publisher requires main branch')
            recover_prepared(local)
            if not sync_remote():
                print('Updated from remote; next scheduled invocation will run the new code.'); return 0
            staging = local / 'public-next'
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(ROOT / 'docs/data', staging)
            result = collect(Path.home() / '.codex', staging, local)
            validate_public(staging)
            if file_hashes(staging) != file_hashes(ROOT / 'docs/data'):
                plan = {'base':git('rev-parse','HEAD'),'files':file_hashes(staging),'message':'data: refresh automation snapshot ' + iso(dt.datetime.now(dt.timezone.utc).timestamp())}
                atomic_json(local / 'prepared.json', plan)
                for file in staging.glob('*.json'):
                    shutil.copy2(file, ROOT / 'docs/data' / file.name)
                for file in (ROOT / 'docs/data').glob('*.json'):
                    if file.name not in plan['files']:
                        file.unlink()
            if git('status','--porcelain','--','docs/data'):
                recover_prepared(local)
                git('push','origin','HEAD:main')
            git('fetch','origin','main')
            if git('rev-parse','HEAD') != git('rev-parse','origin/main'):
                raise RuntimeError('remote parity not verified')
            receipt = {'ok':True,'checked_at':iso(dt.datetime.now(dt.timezone.utc).timestamp()),'commit':git('rev-parse','HEAD'),'collection':result}
            atomic_json(local / 'publish-status.json', receipt)
            print(json.dumps(receipt, ensure_ascii=False)); return 0
        except Exception as exc:
            atomic_json(local / 'publish-status.json', {'ok':False,'checked_at':iso(dt.datetime.now(dt.timezone.utc).timestamp()),'error':str(exc)})
            print(str(exc), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
