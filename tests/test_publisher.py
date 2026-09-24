import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_collector import Fixture
import publish
from install_launchd import proxy_environment


def command(folder, *args):
    return subprocess.run(['git',*args],cwd=folder,check=True,text=True,capture_output=True).stdout.strip()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.repo=self.base/'repo';self.repo.mkdir()
        self.remote=self.base/'remote.git'
        command(self.base,'init','--bare',str(self.remote))
        command(self.repo,'init','-b','main')
        command(self.repo,'config','user.name','Test')
        command(self.repo,'config','user.email','test@example.invalid')
        (self.repo/'.gitignore').write_text('.local/\n')
        (self.repo/'README.md').write_text('initial\n')
        command(self.repo,'add','.');command(self.repo,'commit','-m','initial')
        command(self.repo,'remote','add','origin',str(self.remote))
        command(self.repo,'push','-u','origin','main')
        self.patch=patch.object(publish,'ROOT',self.repo);self.patch.start()

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def test_dirty_tree_rejected_without_staging(self):
        (self.repo/'README.md').write_text('user changes\n')
        with self.assertRaises(RuntimeError):publish.sync_remote()
        self.assertEqual(command(self.repo,'diff','--cached'),'')

    def test_launchd_inherits_only_local_credential_free_proxy(self):
        env={'HTTPS_PROXY':'http://127.0.0.1:10809','HTTP_PROXY':'http://user:secret@127.0.0.1:10809','ALL_PROXY':'http://remote.example:8080','NO_PROXY':'localhost'}
        self.assertEqual(proxy_environment(env),{'HTTPS_PROXY':'http://127.0.0.1:10809','NO_PROXY':'localhost'})

    def test_failed_data_push_retried(self):
        (self.repo/'docs/data').mkdir(parents=True)
        (self.repo/'docs/data/test.json').write_text('{}')
        command(self.repo,'add','docs/data');command(self.repo,'commit','-m','data: refresh automation snapshot test')
        self.assertTrue(publish.sync_remote())
        self.assertEqual(command(self.repo,'rev-parse','HEAD'),command(self.repo,'rev-parse','origin/main'))

    def test_non_data_commit_not_auto_pushed(self):
        (self.repo/'README.md').write_text('new code\n')
        command(self.repo,'add','README.md');command(self.repo,'commit','-m','manual edit')
        with self.assertRaises(RuntimeError):publish.sync_remote()
        self.assertNotEqual(command(self.repo,'rev-parse','HEAD'),command(self.repo,'rev-parse','origin/main'))

    def test_prepared_data_recovers_after_interrupted_commit(self):
        fixture=Fixture(self.base/'fixture');fixture.add_run();fixture.run()
        import shutil
        shutil.copytree(fixture.out,self.repo/'docs/data');fixture.close()
        local=self.repo/'.local';local.mkdir()
        plan={'base':command(self.repo,'rev-parse','HEAD'),'files':publish.file_hashes(self.repo/'docs/data'),'message':'data: refresh automation snapshot test'}
        (local/'prepared.json').write_text(json.dumps(plan))
        publish.recover_prepared(local)
        self.assertEqual(command(self.repo,'status','--porcelain'),'')
        self.assertFalse((local/'prepared.json').exists())
        self.assertTrue(publish.sync_remote())


if __name__=='__main__':unittest.main()
