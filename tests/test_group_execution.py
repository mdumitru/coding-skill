"""Execute group handoffs in fresh processes and one mocked-faur task identity."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_plan_file import RepositoryFixture, run_git
from tests.test_workflow import SCRIPTS, grouped_plan
from tests.test_preview_workflow import plain_repository, calls


FAKE_FAUR = r'''
import json
import os
import subprocess
import sys
from pathlib import Path

config_path = Path(os.environ['GROUP_FAUR_CONFIG'])
config = json.loads(config_path.read_text())
workspace = Path(config['workspace'])
launch = workspace / 'main'
common = str(workspace / '.bare')
args = sys.argv[1:]
operation = args[0]
log = Path(config['log'])
with log.open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'argv': args, 'cwd': str(Path.cwd())}) + '\n')

def git(*argv):
    return subprocess.run(['git', *argv], cwd=launch, capture_output=True, text=True, check=True).stdout.strip()

launch_tree = {'name': 'main', 'path': str(launch), 'branch': 'main', 'head': git('rev-parse', 'main')}
name = 'example-2'
task = workspace / name
worktree = {'name': name, 'path': str(task), 'branch': name, 'head': git('rev-parse', 'main')}
record = config.get('record')
if operation == 'add':
    dry = '--dry-run' in args
    if not dry:
        if record is not None:
            raise SystemExit('a second task must never be created')
        git('worktree', 'add', '-qb', name, str(task))
        record = {'schema_version': 1, 'task_id': 'one-immutable-group-id', 'workspace': {'common_dir': common},
                  'phase': 'ready', 'requested_slug': 'example', 'launch': launch_tree, 'task': worktree}
        config['record'] = record
        config_path.write_text(json.dumps(config))
    response = {'schema_version': 1, 'command': 'add', 'ok': True, 'dry_run': dry,
                'worktree': worktree, 'launch': launch_tree, 'task': record,
                'task_id': record['task_id'] if record else None}
elif operation == 'context':
    use_task = Path.cwd() == task or (len(args) > 2 and args[1] == name)
    response = {'schema_version': 1, 'command': 'context', 'ok': True, 'workspace': {'common_dir': common},
                'worktree': worktree if use_task else launch_tree,
                'task': {'status': 'valid' if use_task else 'none', 'record': record if use_task else None}}
else:
    raise SystemExit('unexpected faur operation')
print(json.dumps(response))
'''


class FreshProcessExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.environment = {**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
                            'XDG_STATE_HOME': str(self.root / 'machine-state')}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, cwd: Path, script: str, *args: str, success: bool = True,
                scripts: Path = SCRIPTS) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([sys.executable, str(scripts / script), *args], cwd=cwd,
                                env=self.environment, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def workflow(self, cwd: Path, operation: str, *args: str, success: bool = True,
                 scripts: Path = SCRIPTS) -> dict[str, object]:
        result = self.command(cwd, 'workflow.py', operation, 'example', *args, success=success, scripts=scripts)
        return json.loads(result.stdout) if success else {'diagnostic': result.stderr}

    def begin_group(self, cwd: Path, group: str, *, scripts: Path = SCRIPTS) -> str:
        result = self.workflow(cwd, 'group-begin', '--group', group, scripts=scripts)
        return result['active_group']['assignment']

    def children(self, cwd: Path, group: str, token: str, *, changed: bool = True,
                 scripts: Path = SCRIPTS) -> dict[str, object]:
        scope = ['--group', group, '--assignment', token]
        for child in range(1, 3):
            handoff = self.command(cwd, 'group_context.py', 'example', *scope, scripts=scripts)
            facts = json.loads(handoff.stdout)
            self.assertEqual(facts['remaining_tasks'][0]['id'], f'child-{group[-1]}-{child}')
            self.workflow(cwd, 'begin', *scope, scripts=scripts)
            if changed:
                (cwd / f'{group}-{child}.txt').write_text(f'{group}/{child}\n')
            result = self.workflow(cwd, 'complete', *scope, scripts=scripts)
        return result

    def checkpoint(self, cwd: Path, group: str, token: str, *, scripts: Path = SCRIPTS) -> dict[str, object]:
        scope = ['--group', group, '--assignment', token]
        self.workflow(cwd, 'group-release', *scope, scripts=scripts)
        return self.workflow(cwd, 'group-complete', *scope, '--accepted', scripts=scripts)

    def test_mocked_faur_two_groups_one_identity_and_persisted_handoff(self) -> None:
        workspace, launch = RepositoryFixture(self.root).faur()
        run_git('config', 'user.name', 'Group Tests', cwd=launch)
        run_git('config', 'user.email', 'group@example.invalid', cwd=launch)
        plan = workspace / '_plans/example.md'
        plan.parent.mkdir()
        plan.write_text(grouped_plan(faur=True))
        fake = self.root / 'fake_faur.py'
        fake.write_text(FAKE_FAUR)
        config = self.root / 'faur_config.json'
        log = self.root / 'faur_calls.jsonl'
        config.write_text(json.dumps({'workspace': str(workspace), 'log': str(log)}))
        self.environment['GROUP_FAUR_CONFIG'] = str(config)
        runner = json.dumps([sys.executable, str(fake)])
        created = self.workflow(launch, 'start', '--faur-argv', runner)
        identity = created['identity']
        task = Path(identity['path'])
        self.assertEqual(task.name, 'example-2')
        token = self.begin_group(task, 'group-1')
        self.children(task, 'group-1', token)
        pending = self.workflow(task, 'report')
        self.assertEqual(pending['pending_groups'], ['group-1', 'group-2'])
        self.assertIn('boundary', self.workflow(task, 'begin', '--group', 'group-1', '--assignment', token, success=False)['diagnostic'])
        self.checkpoint(task, 'group-1', token)
        revision = self.root / 'revision.md'
        revision.write_text(plan.read_text().replace('Use plain text files to persist results.',
                            'Use plain text files.\nApproved interface decision: outputs use UTF-8.'))
        self.workflow(task, 'revise', '--content', str(revision))
        second = self.begin_group(task, 'group-2')
        handoff = self.command(task, 'group_context.py', 'example', '--group', 'group-2', '--assignment', second)
        context = json.loads(handoff.stdout)
        self.assertIn('UTF-8', context['shared_context'])
        self.assertEqual([t['id'] for t in context['remaining_tasks']], ['child-2-1', 'child-2-2'])
        self.assertNotIn('stdout', handoff.stdout)
        self.assertIn('stale', self.workflow(task, 'begin', '--group', 'group-2', '--assignment', token, success=False)['diagnostic'])
        self.children(task, 'group-2', second)
        self.checkpoint(task, 'group-2', second)
        audited = self.workflow(task, 'audit')
        self.assertEqual(audited['identity'], identity)
        self.assertEqual(len(audited['records']), 4)
        self.assertEqual(len(audited['group_records']), 2)
        for first, following in zip(audited['records'], audited['records'][1:]):
            self.assertEqual(first['end_head'], following['start_head'])
        resumed = self.workflow(task, 'start', '--faur-argv', runner)
        self.assertEqual(resumed['identity'], identity)
        operations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(e['argv'][0] == 'add' and '--dry-run' not in e['argv'] for e in operations), 1)
        self.assertGreater(len({e['pid'] for e in operations}), 10)
        self.assertEqual(self.workflow(task, 'report')['pending_groups'], [])
        self.assertEqual(worktree_count(launch), 2)

    def test_combined_preview_waits_for_final_group_checkpoint_and_starts_once(self) -> None:
        repo = plain_repository(self.root)
        plan = repo / '_plans/example.md'
        plan.write_text(grouped_plan())
        self.command(repo, 'preview.py', 'intent', '--execute-slug', 'example')
        self.workflow(repo, 'start')
        token = self.begin_group(repo, 'group-1')
        self.children(repo, 'group-1', token, changed=False)
        self.command(repo, 'preview.py', 'run', '--execute-slug', 'example', success=False)
        self.checkpoint(repo, 'group-1', token)
        second = self.begin_group(repo, 'group-2')
        self.children(repo, 'group-2', second)
        self.assertEqual(self.workflow(repo, 'report')['run_intent'], 'pending')
        self.command(repo, 'preview.py', 'run', '--execute-slug', 'example', success=False)
        state_dir = repo / '.worktree-runs/example/state'
        self.assertEqual(calls(state_dir, 'start'), [])
        self.checkpoint(repo, 'group-2', second)
        self.workflow(repo, 'audit')
        first = self.command(repo, 'preview.py', 'run', '--execute-slug', 'example')
        again = self.command(repo, 'preview.py', 'run', '--execute-slug', 'example')
        self.assertEqual(json.loads(first.stdout)['runtime_id'], json.loads(again.stdout)['runtime_id'])
        self.assertEqual(len(calls(state_dir, 'start')), 1)
        self.assertEqual(self.workflow(repo, 'report')['run_intent'], 'discharged')
        self.workflow(repo, 'audit')

    def test_checkpoint_crash_after_markdown_recovers_in_new_process(self) -> None:
        repo = plain_repository(self.root)
        plan = repo / '_plans/example.md'
        plan.write_text(grouped_plan())
        self.workflow(repo, 'start')
        token = self.begin_group(repo, 'group-1')
        self.children(repo, 'group-1', token)
        self.workflow(repo, 'group-release', '--group', 'group-1', '--assignment', token)
        code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import workflow
from unittest.mock import patch
real = workflow.save
def crash(path, state):
    if state.active_group is None:
        raise OSError('interrupted final checkpoint write')
    real(path, state)
with patch('workflow.save', side_effect=crash):
    workflow.group_complete('example', 'group-1', sys.argv[2], accepted=True)
'''
        interrupted = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), token], cwd=repo,
                                      env=self.environment, capture_output=True, text=True)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertIn('interrupted final checkpoint', interrupted.stderr)
        self.assertIn('- [x] Group 1', plan.read_text())
        self.assertIn('transaction', self.workflow(repo, 'group-next', success=False)['diagnostic'])
        result = self.workflow(repo, 'group-complete', '--group', 'group-1', '--assignment', token, '--accepted')
        self.assertEqual(len(result['group_records']), 1)
        self.assertEqual(len(result['records']), 2)
        second = self.begin_group(repo, 'group-2')
        self.children(repo, 'group-2', second, changed=False)
        self.checkpoint(repo, 'group-2', second)
        self.workflow(repo, 'audit')

    def test_installed_packages_execute_groups_in_both_isolated_harnesses(self) -> None:
        from tests.test_installer import IsolatedHome
        home = IsolatedHome(self.root / 'isolated-home')
        home.root.mkdir()
        home.initialize_targets()
        home.run('--dry-run')
        home.run()
        home.run('--check')
        for target in (home.claude, home.codex):
            case = self.root / target.name
            case.mkdir()
            repo = plain_repository(case)
            plan = repo / '_plans/example.md'
            plan.write_text(grouped_plan())
            scripts = target / 'skills/task-workflow/scripts'
            self.assertTrue((scripts / 'group_context.py').stat().st_mode & 0o111)
            self.assertTrue((scripts.parent / 'group-execution.md').is_file())
            self.workflow(repo, 'start', scripts=scripts)
            first = self.begin_group(repo, 'group-1', scripts=scripts)
            self.children(repo, 'group-1', first, scripts=scripts)
            self.checkpoint(repo, 'group-1', first, scripts=scripts)
            second = self.begin_group(repo, 'group-2', scripts=scripts)
            self.children(repo, 'group-2', second, changed=False, scripts=scripts)
            self.checkpoint(repo, 'group-2', second, scripts=scripts)
            self.workflow(repo, 'audit', scripts=scripts)
        before = set(home.backups.glob('*'))
        again = home.run()
        self.assertIn('up to date', again.stdout)
        self.assertEqual(set(home.backups.glob('*')), before)
        home.run('--check')


def worktree_count(repository: Path) -> int:
    result = subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=repository,
                            capture_output=True, text=True, check=True)
    # The bare repository is also listed; count only checked-out identities.
    return sum(line.startswith('branch ') for line in result.stdout.splitlines())


if __name__ == '__main__':
    unittest.main()
