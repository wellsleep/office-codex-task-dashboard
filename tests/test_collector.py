import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import classify, clean, collect, iso, parse_rollout
from publish import validate_public

NOW = 1800000000


class Fixture:
    def __init__(self, root):
        self.home = root / 'codex'
        self.out = root / 'public'
        self.local = root / 'local'
        (self.home / 'sqlite').mkdir(parents=True)
        self.app = sqlite3.connect(self.home / 'sqlite/codex-dev.db')
        self.app.execute('CREATE TABLE automations(id TEXT,last_run_at INTEGER,next_run_at INTEGER,target_thread_id TEXT)')
        self.app.execute('CREATE TABLE automation_runs(thread_id TEXT,automation_id TEXT,created_at INTEGER,archived_assistant_message TEXT)')
        self.threads = sqlite3.connect(self.home / 'state_99.sqlite')
        self.threads.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,first_user_message TEXT,created_at INTEGER,source TEXT)')
        self.add_task('daily')

    def add_task(self, aid, kind='cron', target=None, created=NOW-86400*30):
        p = self.home / 'automations' / aid / 'automation.toml'
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('id = '+json.dumps(aid)+'\nname = '+json.dumps(aid)+'\nstatus = "ACTIVE"\nkind = '+json.dumps(kind)+'\nrrule = "FREQ=DAILY;BYHOUR=3;BYMINUTE=0"\ncreated_at = '+str(created*1000)+'\n')
        self.app.execute('INSERT INTO automations VALUES (?,?,?,?)',(aid,None,(NOW+86400)*1000,target)); self.app.commit()

    def add_run(self, aid='daily',tid='r1',index=True,heartbeat=False,started=NOW-600,message='本次状态：success。',source='vscode'):
        path = self.home / (tid + '.jsonl')
        events=[{'timestamp':iso(started),'type':'event_msg','payload':{'type':'task_started','turn_id':tid}}]
        if heartbeat:
            events.append({'timestamp':iso(started),'type':'response_item','payload':{'role':'user','content':[{'type':'input_text','text':'<heartbeat>\n<automation_id>'+aid+'</automation_id>\n</heartbeat>'}]}})
        events.append({'timestamp':iso(started+100),'type':'event_msg','payload':{'type':'task_complete','turn_id':tid,'last_agent_message':message}})
        path.write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in events)+'\n')
        header = '普通对话' if heartbeat else 'Automation: task\nAutomation ID: '+aid+'\n'
        self.threads.execute('INSERT INTO threads VALUES (?,?,?,?,?)',(tid,str(path),header,started,source)); self.threads.commit()
        if index:
            self.app.execute('INSERT INTO automation_runs(thread_id,automation_id,created_at,archived_assistant_message) VALUES (?,?,?,?)',(tid,aid,started*1000,'')); self.app.commit()
        return path

    def run(self, now=NOW):
        return collect(self.home,self.out,self.local,now=now)

    def records(self):
        m=json.loads((self.out/'manifest.json').read_text())
        return [r for s in m['shards'] for r in json.loads((self.out/s['file']).read_text())]

    def close(self):
        self.app.close();self.threads.close()


class ClassificationTests(unittest.TestCase):
    def test_explicit_success_despite_resolved_network_error(self):
        self.assertEqual(classify('成功：正式快照已完成并发布。\n沙箱 DNS 失败后，授权网络重试解决。')[0],'success')

    def test_explicit_failure_despite_passed_preflight(self):
        self.assertEqual(classify('本次状态：failed（零写入）。\nBot预检通过，DNS解析失败。')[0],'failed')

    def test_explicit_partial(self):
        self.assertEqual(classify('重试完成，状态为 partial（部分成功）。\n其余内容已发送。')[0],'partial')

    def test_invalid_messages_are_partial(self):
        self.assertEqual(classify('归档完成。\n3 个合并包含 6 条无效消息。')[0],'partial')

    def test_zero_failures_not_failure(self):
        self.assertEqual(classify('成功：快照完成。\n展开成功 700，失败 0。')[0],'success')

    def test_missing_result_unknown(self):
        self.assertEqual(classify('')[0],'unknown')
        self.assertEqual(classify('PENDING_REVIEW ACCEPTED ARCHIVED')[0],'unknown')

    def test_no_receipt_and_no_push(self):
        self.assertEqual(classify('Receipt：未生成\nPush：未执行')[0],'failed')

    def test_redaction(self):
        text='[文件](/Users/alice/private.txt) token=secretvalue 10.1.2.3 user@example.com https://host/p?token=x ghp_'+'a'*30+' om_'+'b'*30
        output=clean(text)
        for raw in ('/Users/','secretvalue','10.1.2.3','user@example.com','https://','ghp_','om_'):
            self.assertNotIn(raw,output)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.f=Fixture(Path(self.temp.name))
    def tearDown(self):
        self.f.close();self.temp.cleanup()

    def test_automatic_new_task_discovery(self):
        self.f.add_run();self.f.run()
        self.f.add_task('brand-new');self.f.add_run('brand-new','r2')
        result=self.f.run(NOW+1800)
        self.assertEqual((result['tasks'],result['runs']),(2,2));validate_public(self.f.out)

    def test_duplicate_index_and_header_count_once(self):
        self.f.add_run();self.assertEqual(self.f.run()['runs'],1)

    def test_quiet_run_recovered_from_header(self):
        self.f.add_run(index=False);self.assertEqual(self.f.run()['runs'],1)
        self.assertEqual(self.f.records()[0]['source'],'session_header')

    def test_guardian_not_imported(self):
        self.f.add_run(index=False,source='{"subagent":{"other":"guardian"}}')
        self.assertEqual(self.f.run()['runs'],0)

    def test_id_reuse_excludes_previous_task(self):
        self.f.add_task('reused',created=NOW-1000)
        self.f.add_run('reused','old',started=NOW-86400)
        self.assertEqual(self.f.run()['runs'],0)

    def test_heartbeat_and_regular_chat_are_separate(self):
        self.f.add_task('beat',kind='heartbeat',target='thread')
        path=self.f.add_run('beat','thread',heartbeat=True,index=False)
        with path.open('a') as f:
            f.write(json.dumps({'timestamp':iso(NOW-200),'type':'event_msg','payload':{'type':'task_started','turn_id':'manual'}})+'\n')
            f.write(json.dumps({'timestamp':iso(NOW-100),'type':'event_msg','payload':{'type':'task_complete','turn_id':'manual','last_agent_message':'failed'}})+'\n')
        self.assertEqual(self.f.run()['runs'],1)
        self.assertEqual(self.f.records()[0]['outcome'],'success')

    def test_new_optional_columns_are_ignored(self):
        self.f.app.execute('ALTER TABLE automation_runs ADD COLUMN new_field TEXT');self.f.app.commit()
        self.f.add_run();self.assertEqual(self.f.run()['runs'],1)

    def test_index_schema_missing_falls_back_with_warning(self):
        self.f.add_run(index=False)
        self.f.app.execute('DROP TABLE automation_runs');self.f.app.commit()
        result=self.f.run();self.assertEqual(result['runs'],1);self.assertTrue(result['warnings'])

    def test_no_source_does_not_overwrite_good_snapshot(self):
        self.f.add_run();self.f.run();before=(self.f.out/'manifest.json').read_bytes()
        self.f.threads.execute('ALTER TABLE threads RENAME COLUMN rollout_path TO changed_path');self.f.threads.commit()
        with self.assertRaises(RuntimeError):self.f.run()
        self.assertEqual((self.f.out/'manifest.json').read_bytes(),before)

    def test_disappeared_log_preserves_flagged_history(self):
        path=self.f.add_run();self.f.run();path.unlink();self.f.run(NOW+1800)
        self.assertTrue(self.f.records()[0]['evidence_stale'])
        self.assertEqual(self.f.records()[0]['outcome'],'success')

    def test_no_change_does_not_publish_until_two_hour_heartbeat(self):
        self.f.add_run();self.f.run()
        self.assertFalse(self.f.run(NOW+1800)['publish'])
        self.assertFalse(self.f.run(NOW+3600)['publish'])
        self.assertFalse(self.f.run(NOW+7199)['publish'])
        self.assertTrue(self.f.run(NOW+7200)['publish'])

    def test_source_changes_do_not_duplicate_identity(self):
        self.f.add_run();self.f.run()
        self.f.app.execute('DELETE FROM automation_runs');self.f.app.commit()
        self.assertEqual(self.f.run(NOW+1800)['runs'],1)

    def test_source_files_unmodified(self):
        self.f.add_run()
        before={p:p.read_bytes() for p in self.f.home.rglob('*') if p.is_file()}
        self.f.run()
        self.assertTrue(all(p.read_bytes()==b for p,b in before.items()))

    def test_sensitive_public_data_rejected(self):
        self.f.add_run();self.f.run()
        (self.f.out/'unapproved.json').write_text('{"token":"ghp_'+'a'*35+'"}')
        with self.assertRaises(ValueError):validate_public(self.f.out)


if __name__=='__main__':unittest.main()
