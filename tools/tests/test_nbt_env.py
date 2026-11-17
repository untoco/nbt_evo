"""Validate launcher paths, argument forwarding and error handling without deployment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

LAUNCHER = Path(__file__).parents[1] / 'nbt-env'


class EnvironmentLauncher(unittest.TestCase):
    def invoke(self, action, code=0, extra=()):
        with tempfile.TemporaryDirectory(prefix='nbt environment ') as directory:
            root = Path(directory)
            (root / 'tools').mkdir()
            (root / 'fake-bin').mkdir()
            launcher = root / 'tools/nbt-env'
            shutil.copy2(LAUNCHER, launcher)
            result_file = root / 'captured.json'
            fake = root / 'fake-bin/ansible-playbook'
            fake.write_text(
                '#!' + sys.executable + '\n'
                'import json,os,sys\n'
                'from pathlib import Path\n'
                "Path(os.environ['CAPTURE']).write_text(json.dumps({'args':sys.argv[1:],"
                "'log':os.environ['ANSIBLE_LOG_PATH'],'temp':os.environ['ANSIBLE_LOCAL_TEMP']}))\n"
                "sys.exit(int(os.environ['RESULT']))\n"
            )
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(fake.parent) + os.pathsep + os.environ['PATH'],
                       CAPTURE=str(result_file), RESULT=str(code))
            proc = subprocess.run([str(launcher), action, *extra], env=env,
                                  capture_output=True, text=True, cwd='/')
            data = json.loads(result_file.read_text()) if result_file.exists() else None
            if data:
                self.assertTrue(Path(data['temp']).is_dir())
                self.assertEqual(Path(data['log']).parent, root / '.build/bootstrap')
                self.assertEqual((root / '.build/bootstrap/latest.log').resolve(), Path(data['log']).resolve())
                self.assertIn(str(root / 'automation/ansible/environment.yml'), data['args'])
            return proc, data

    def test_check_runs_only_verification_and_preserves_extra_arguments(self):
        proc, data = self.invoke('check', extra=('-e', '{"nbt_offline":true}'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data['args'][-4:], ['--tags', 'verify', '-e', '{"nbt_offline":true}'])

    def test_setup_returns_ansible_failure(self):
        proc, data = self.invoke('setup', code=2)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn('--tags', data['args'])

    def test_plan_only_lists_tasks(self):
        proc, data = self.invoke('plan')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--list-tasks', data['args'])

    def test_unknown_action_does_not_invoke_ansible(self):
        proc, data = self.invoke('invalid')
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(data)


if __name__ == '__main__':
    unittest.main()
