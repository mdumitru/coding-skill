"""Finish wrapper tests: dry-run, owned teardown, and real faur finish ordering."""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from tests.test_plan_file import RepositoryFixture, run_git
from tests.test_preview_workflow import PreviewTestCase, calls, install_tools
from tests.test_workflow import executable_plan, working_directory

import preview
import preview_protocol as pp
import workflow

FINISH = Path(__file__).parents[1] / 'skills/worktree-workflow/scripts/finish.py'

FAKE_FAUR = r'''
import json, os, subprocess, sys
from pathlib import Path
config = json.loads(Path(os.environ['FAKE_FAUR_CONFIG']).read_text())
args = sys.argv[1:]
entry = {'args': args}
if args[0] == 'context' and len(args) == 2 and not Path.cwd().resolve().is_relative_to(config['task_path']):
    print(json.dumps(config['launch_context']))
elif args[0] == 'add':
    dry = '--dry-run' in args
    if not dry:
        subprocess.run(['git', 'worktree', 'add', '-qb', config['created']['worktree']['branch'], config['task_path']], cwd=config['launch'], check=True, capture_output=True)
    print(json.dumps({**config['created'], 'dry_run': dry}))
elif args[0] == 'context':
    if not Path(config['task_path']).exists():
        print(json.dumps({'schema_version': 1, 'command': 'context', 'ok': False, 'error': 'worktree missing'}))
        sys.exit(1)
    print(json.dumps(config['context']))
elif args[0] == 'finish':
    dry = '--dry-run' in args
    record = Path(config['record'])
    entry['record_phase'] = json.loads(record.read_text())['phase'] if record.exists() else None
    entry['running'] = Path(config['running']).exists()
    fail = os.environ.get('FAKE_FAUR_FAIL', '')
    if (dry and fail == 'dry-run') or (not dry and fail == 'finish'):
        with open(os.environ['FAKE_FAUR_LOG'], 'a') as stream:
            stream.write(json.dumps({**entry, 'failed': True}) + '\n')
        print('conflict in app.txt', file=sys.stderr)
        sys.exit(1)
    if not dry:
        subprocess.run(['git', 'worktree', 'remove', '--force', config['task_path']], cwd=config['launch'], check=True, capture_output=True)
    print('finished' if not dry else 'would finish')
with open(os.environ['FAKE_FAUR_LOG'], 'a') as stream:
    stream.write(json.dumps(entry) + '\n')
'''


def load_finish() -> ModuleType:
    spec = importlib.util.spec_from_file_location('finish_helper', FINISH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


finish = load_finish()


class FinishTestCase(PreviewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace, self.launch = RepositoryFixture(self.root).faur()
        run_git('config', 'user.name', 'Finish Tests', cwd=self.launch)
        run_git('config', 'user.email', 'test@example.invalid', cwd=self.launch)
        (self.launch / '.gitignore').write_text('__pycache__/\n')
        install_tools(self.launch)
        run_git('add', '.', cwd=self.launch)
        run_git('commit', '-qm', 'add preview tools', cwd=self.launch)
        (self.workspace / '_plans').mkdir()
        (self.workspace / '_plans/example.md').write_text(executable_plan(faur=True))
        self.task = self.workspace / 'example-2'
        run_git('worktree', 'add', '-qb', 'example-2', str(self.task), cwd=self.launch)
        common = str(self.workspace / '.bare')
        head = workflow.git_output(self.launch, 'rev-parse', 'HEAD')
        launch_tree = {'path': str(self.launch), 'branch': 'main', 'name': 'main', 'head': head}
        self.task_record = {'schema_version': 1, 'task_id': 'immutable-finish-id', 'workspace': {'root': str(self.workspace), 'common_dir': common},
                            'requested_slug': 'example', 'phase': 'ready', 'launch': launch_tree,
                            'task': {'name': 'example-2', 'path': str(self.task), 'branch': 'example-2'}}
        worktree = {'name': 'example-2', 'path': str(self.task), 'branch': 'example-2', 'head': head}
        self.launch_context = {'schema_version': 1, 'command': 'context', 'ok': True, 'workspace': {'root': str(self.workspace), 'common_dir': common, 'faur': True},
                               'worktree': launch_tree, 'task': {'status': 'none', 'record': None}}
        self.task_context = {**self.launch_context, 'worktree': worktree, 'task': {'status': 'valid', 'record': self.task_record, 'error': None}}
        created = {'schema_version': 1, 'command': 'add', 'ok': True, 'dry_run': False, 'worktree': worktree, 'task_id': 'immutable-finish-id', 'task': self.task_record, 'launch': launch_tree}

        def fake_faur(runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
            if args[0] == 'add':
                return {**created, 'dry_run': '--dry-run' in args}
            return self.launch_context if cwd == self.launch and len(args) == 2 else self.task_context
        self.faur_patch = patch('workflow.run_faur', side_effect=fake_faur)
        self.faur_patch.start()
        with working_directory(self.launch):
            workflow.start('example', runner=['fixture'])
        self.runs = self.workspace / '_runs/example'
        self.state_dir = self.runs / 'state'
        self.registry = self.root / 'state/coding-skill/preview-hostnames'
        self.log = self.root / 'faur.log'
        script = self.root / 'fake_faur.py'
        script.write_text(FAKE_FAUR)
        config = self.root / 'faur.json'
        config.write_text(json.dumps({'context': self.task_context, 'task_path': str(self.task), 'launch': str(self.launch),
                                      'record': str(self.runs / 'runtime.json'), 'running': str(self.state_dir / 'running')}))
        os.environ.update({'FAKE_FAUR_CONFIG': str(config), 'FAKE_FAUR_LOG': str(self.log), 'FAKE_FAUR_FAIL': ''})
        self.faur_argv = json.dumps([sys.executable, str(script)])

    def tearDown(self) -> None:
        self.faur_patch.stop()
        super().tearDown()

    def start_preview(self) -> dict[str, object]:
        with working_directory(self.launch):
            return preview.run('example', [], [])

    def run_finish(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(FINISH), 'example-2', '--faur-argv', self.faur_argv, *extra],
                              cwd=self.launch, capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': ''})

    def faur_log(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def finish_calls(self) -> list[list[str]]:
        return [e['args'] for e in self.faur_log() if e['args'][0] == 'finish']

    def record(self) -> dict[str, object]:
        return json.loads((self.runs / 'runtime.json').read_text())


class FinishOrderTests(FinishTestCase):
    def test_teardown_precedes_real_finish_and_cleans_owned_state(self) -> None:
        self.start_preview()
        remote = 'refs/remotes/origin/example-2'
        run_git('update-ref', remote, 'HEAD', cwd=self.launch)
        result = self.run_finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['teardown'][0]['result'], 'torn down')
        real = [e for e in self.faur_log() if e['args'][0] == 'finish' and '--dry-run' not in e['args']]
        self.assertEqual(len(real), 1)
        self.assertEqual((real[0]['record_phase'], real[0]['running']), ('torn-down', False))
        dry = [e for e in self.faur_log() if '--dry-run' in e['args']][0]
        self.assertEqual((dry['record_phase'], dry['running']), ('running', True))
        self.assertLess(self.faur_log().index(dry), self.faur_log().index(real[0]))
        self.assertFalse(self.task.exists())
        self.assertFalse(self.runs.exists())
        self.assertFalse((self.registry / 'example.localhost.json').exists())
        self.assertEqual(subprocess.run(['git', 'show-ref', '--verify', '--quiet', remote], cwd=self.launch).returncode, 0)

    def test_dry_run_has_no_cleanup_effects(self) -> None:
        self.start_preview()
        result = self.run_finish('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['teardown'], 'not run during dry-run')
        self.assertEqual(self.finish_calls(), [['finish', 'example-2', '--dry-run']])
        self.assertEqual(self.record()['phase'], 'running')
        self.assertEqual(calls(self.state_dir, 'stop'), [])
        self.assertTrue((self.registry / 'example.localhost.json').exists())

    def test_failed_dry_run_prevents_teardown(self) -> None:
        self.start_preview()
        os.environ['FAKE_FAUR_FAIL'] = 'dry-run'
        result = self.run_finish()
        self.assertEqual(result.returncode, 2)
        self.assertIn('conflict in app.txt', result.stderr)
        self.assertEqual(calls(self.state_dir, 'stop'), [])
        self.assertEqual(self.record()['phase'], 'running')

    def test_branch_is_propagated_to_both_finish_calls(self) -> None:
        result = self.run_finish('--branch', 'develop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.finish_calls(), [['finish', 'example-2', '--branch', 'develop', '--dry-run'], ['finish', 'example-2', '--branch', 'develop']])

    def test_no_preview_finishes_normally(self) -> None:
        result = self.run_finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['teardown'], [])
        self.assertFalse(self.task.exists())

    def test_stopped_preview_with_retained_data_is_torn_down(self) -> None:
        self.start_preview()
        with working_directory(self.launch):
            preview.stop_runtime(preview.resolve_target('example'), teardown=False, timeout=30)
        self.assertTrue((self.state_dir / 'data').exists())
        result = self.run_finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runs.exists())


class FinishFailureTests(FinishTestCase):
    def test_teardown_failure_blocks_removal_then_retry_succeeds(self) -> None:
        self.start_preview()
        self.mode('stop-fail')
        result = self.run_finish()
        self.assertEqual(result.returncode, 2)
        self.assertIn('real finish blocked', result.stderr)
        self.assertEqual(self.finish_calls(), [['finish', 'example-2', '--dry-run']])
        self.assertTrue(self.task.exists())
        record = self.record()
        self.assertEqual(record['phase'], 'tearing-down')
        self.assertIn('did not confirm teardown', record['error'])
        self.assertTrue((self.registry / 'example.localhost.json').exists())
        self.mode('')
        retry = self.run_finish()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse(self.task.exists())

    def test_real_finish_failure_retains_state_and_retry_is_safe(self) -> None:
        self.start_preview()
        os.environ['FAKE_FAUR_FAIL'] = 'finish'
        result = self.run_finish()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.task.exists())
        self.assertEqual(self.record()['phase'], 'torn-down')
        stops = len(calls(self.state_dir, 'stop'))
        os.environ['FAKE_FAUR_FAIL'] = ''
        retry = self.run_finish()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)['teardown'][0]['result'], 'already torn down')
        self.assertFalse(self.task.exists())
        self.assertFalse(self.runs.exists())
        self.assertEqual(stops, 1)

    def test_missing_stop_tool_and_adapter_change_block_finish(self) -> None:
        self.start_preview()
        (self.task / pp.TOOLS_DIR / 'stop').unlink()
        result = self.run_finish()
        self.assertEqual(result.returncode, 2)
        self.assertIn('worktree-tools/stop', result.stderr)
        run_git('checkout', '--', '.', cwd=self.task)
        (self.task / pp.TOOLS_DIR / pp.PROTOCOL_FILE).write_text(json.dumps({'schema_version': 1, 'adapter': 'other'}))
        result = self.run_finish()
        self.assertEqual(result.returncode, 2)
        self.assertIn('differs from recorded fake', result.stderr)
        self.assertEqual([c for c in self.finish_calls() if '--dry-run' not in c], [])
        self.assertTrue(self.task.exists())

    def test_malformed_or_inconsistent_manifest_runs_no_commands(self) -> None:
        self.start_preview()
        original = self.record()
        for change in ({'state_dir': '/tmp/elsewhere'}, {'runtime_id': 'rt-forged'}, {'phase': 'exploding'}, {'command': 'rm -rf /'}):
            with self.subTest(change=change):
                (self.runs / 'runtime.json').write_text(json.dumps({**original, **change}))
                result = self.run_finish()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls(self.state_dir, 'stop'), [])
                self.assertEqual(self.finish_calls(), [])
        (self.runs / 'runtime.json').write_text('{corrupt')
        self.assertEqual(self.run_finish().returncode, 2)
        self.assertEqual((self.runs / 'runtime.json').read_text(), '{corrupt')

    def test_unrelated_runtime_and_claim_are_untouched(self) -> None:
        self.start_preview()
        other = self.workspace / '_runs/other-plan'
        (other / 'state').mkdir(parents=True)
        foreign = {**self.record(), 'task_id': 'another-task', 'plan_slug': 'other-plan', 'hostname': 'other-plan.localhost',
                   'runtime_id': pp.runtime_id_for(str(self.workspace / '.bare'), 'another-task', 'other-plan'),
                   'worktree': str(self.workspace / 'other'), 'state_dir': str(other / 'state')}
        (other / 'runtime.json').write_text(json.dumps(foreign))
        claim = pp.Claim('other-plan.localhost', foreign['runtime_id'], 'another-task', foreign['worktree'], foreign['common_dir'], str(other / 'runtime.json'))
        pp.claim_hostname(self.registry, claim)
        before = (other / 'runtime.json').read_text()
        result = self.run_finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((other / 'runtime.json').read_text(), before)
        self.assertEqual(pp.read_claims(self.registry, 'other-plan.localhost'), [claim])


class ReaderContractTests(unittest.TestCase):
    """Writer (task-workflow) and reader (worktree-workflow) must not drift."""

    def test_record_fields_constants_and_registry_agree(self) -> None:
        self.assertEqual(finish.RECORD_FIELDS, tuple(f.name for f in dataclasses.fields(pp.RuntimeRecord)))
        self.assertEqual(finish.RECORD_PHASES, pp.RECORD_PHASES)
        for name in ('SCHEMA_VERSION', 'TOOLS_DIR', 'PROTOCOL_FILE', 'FAUR_RUNS', 'RECORD_FILE', 'RUNTIME_ENV'):
            self.assertEqual(getattr(finish, name), getattr(pp, name), name)
        self.assertEqual(finish.runtime_id_for('/c', 't', 'slug'), pp.runtime_id_for('/c', 't', 'slug'))
        env = {'XDG_STATE_HOME': '/state', 'HOME': '/home/x'}
        self.assertEqual(finish.registry_dir(env), Path('/state/coding-skill/preview-hostnames'))
        self.assertEqual(finish.registry_dir({'HOME': '/home/x'}), Path('/home/x/.local/state/coding-skill/preview-hostnames'))

    def test_teardown_argv_matches_writer_lifecycle_argv(self) -> None:
        identity = pp.make_identity('example', 'task', '/w/example-2', '/w/.bare')
        record = finish.Record(1, identity.runtime_id, 'example', identity.hostname, 'task', identity.worktree, identity.common_dir, 'fake', '/w/_runs/example/state', 'running')
        self.assertEqual(finish.teardown_argv(Path('/w/example-2/worktree-tools/stop'), record),
                         pp.lifecycle_argv(Path('/w/example-2/worktree-tools/stop'), identity, Path('/w/_runs/example/state'), '--teardown'))

    def test_finish_helper_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(FINISH.read_text())
        modules = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(modules - set(sys.stdlib_module_names), set())

    def test_response_and_ownership_rules(self) -> None:
        identity = pp.make_identity('example', 'task', '/w/example-2', '/w/.bare')
        record = finish.Record(1, identity.runtime_id, 'example', identity.hostname, 'task', identity.worktree, identity.common_dir, 'fake', '/w/_runs/example/state', 'running')
        task = finish.TaskFacts('example-2', 'task', Path('/w/example-2'), Path('/w'), '/w/.bare')
        self.assertIsNone(finish.ownership_error(record, task, Path('/w/_runs/example')))
        self.assertIn('state_dir', finish.ownership_error(dataclasses.replace(record, state_dir='/x'), task, Path('/w/_runs/example')))
        self.assertIn('names plan', finish.ownership_error(record, task, Path('/w/_runs/other')))
        good = {'schema_version': 1, 'operation': 'stop', 'success': True, 'status': 'absent', 'plan_slug': 'example', 'task_id': 'task',
                'worktree': identity.worktree, 'runtime_id': identity.runtime_id}
        self.assertIsNone(finish.response_error(good, record))
        for bad in ({**good, 'status': 'running'}, {**good, 'success': False}, {**good, 'task_id': 'x'}, {**good, 'operation': 'start'}, {**good, 'schema_version': 2}, None):
            with self.subTest(bad=bad):
                self.assertIsNotNone(finish.response_error(bad, record))


if __name__ == '__main__':
    unittest.main()
