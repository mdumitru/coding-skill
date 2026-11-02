"""Fake-adapter integration tests for preview orchestration from recorded plan identity."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_plan_file import RepositoryFixture, run_git
from tests.test_workflow import SCRIPTS, executable_plan, working_directory

import preview
import preview_protocol as pp
import workflow
from state_io import StateError

FAKE_ADAPTER = r'''
"""Disposable lifecycle adapter driven by FAKE_PREVIEW_* environment variables."""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def main(operation: str) -> None:
    parser = argparse.ArgumentParser()
    for name in ('--plan-slug', '--hostname', '--task-id', '--state-dir'):
        parser.add_argument(name, required=True)
    parser.add_argument('--json', action='store_true')
    if operation == 'logs':
        parser.add_argument('--lines', type=int, required=True)
        parser.add_argument('--follow', action='store_true')
    if operation == 'stop':
        parser.add_argument('--teardown', action='store_true')
    parser.add_argument('rest', nargs='*')
    args = parser.parse_args()
    state = Path(args.state_dir)
    modes = set(filter(None, os.environ.get('FAKE_PREVIEW_MODE', '').split(',')))
    with (state / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'operation': operation, 'argv': sys.argv[1:]}) + '\n')
    port = int(os.environ.get('FAKE_PREVIEW_PORT', '8080'))
    services = [{'name': 'web', 'url': f'http://{args.hostname}:{port}/', 'port': port}]

    def respond(*, success=True, status='running', ready=True, services=services, detail='ok', details=None, code=0):
        envelope = {'schema_version': 1, 'operation': operation, 'success': success, 'plan_slug': args.plan_slug,
                    'task_id': args.task_id, 'worktree': str(Path.cwd().resolve()), 'runtime_id': os.environ['WORKTREE_RUNTIME_ID'],
                    'status': status, 'readiness': {'ready': ready, 'detail': detail}, 'services': services, 'details': details or {}}
        if os.environ.get('FAKE_PREVIEW_PAIRING'):
            envelope['pairing'] = {'group': os.environ['FAKE_PREVIEW_PAIRING']}
        print(json.dumps(envelope))
        sys.exit(code)

    def launch() -> None:
        (state / 'resource').write_text('owned')
        if operation in modes and 'crash' in modes:
            sys.stderr.write('boom password=hunter2\n')
            sys.exit(1)
        if 'garbage' in modes:
            print('not json')
            sys.exit(1)
        if 'not-ready' in modes:
            respond(status='starting', ready=False, detail='warming up')
        if not (state / 'data').exists():
            (state / 'data').write_text('fresh')
        (state / 'running').write_text('1')
        source = Path('app.txt').read_text() if Path('app.txt').exists() else None
        respond(details={'source': source, 'start_args': args.rest, 'db_password': 'secret', 'data': (state / 'data').read_text()})

    if operation == 'start':
        launch()
    if operation == 'status':
        running = (state / 'running').exists()
        respond(status='running' if running else 'stopped', ready=running, services=services if running else [])
    if operation == 'logs':
        print('\n'.join(f'line {n}' for n in range(args.lines * 2)))
        sys.exit(0)
    if operation == 'stop':
        if 'stop-fail' in modes:
            sys.stderr.write('cannot stop\n')
            respond(success=False, status='running', detail='still busy', code=1)
        (state / 'running').unlink(missing_ok=True)
        if args.teardown:
            for name in ('resource', 'data'):
                (state / name).unlink(missing_ok=True)
        respond(status='absent' if args.teardown else 'stopped', ready=False, services=[])
    if operation == 'reset':
        for name in ('running', 'data'):
            (state / name).unlink(missing_ok=True)
        launch()
'''

PROTOCOL = {'schema_version': 1, 'adapter': 'fake', 'start_arguments': [{'name': '--seed', 'value': True}]}


def install_tools(root: Path, protocol: dict[str, object] | None = None) -> None:
    tools = root / pp.TOOLS_DIR
    tools.mkdir(exist_ok=True)
    (tools / 'fake_adapter.py').write_text(FAKE_ADAPTER)
    (tools / pp.PROTOCOL_FILE).write_text(json.dumps(protocol or PROTOCOL))
    for operation in pp.LIFECYCLE:
        tool = tools / operation
        tool.write_text(f'#!{sys.executable}\nimport os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\nimport fake_adapter\nfake_adapter.main({operation!r})\n')
        tool.chmod(0o755)


def calls(state_dir: Path, operation: str | None = None) -> list[dict[str, object]]:
    path = state_dir / 'calls.jsonl'
    entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [e for e in entries if operation is None or e['operation'] == operation]


def plain_repository(root: Path, *, count: int = 1) -> Path:
    repo = RepositoryFixture(root).plain()
    run_git('config', 'user.name', 'Preview Tests', cwd=repo)
    run_git('config', 'user.email', 'test@example.invalid', cwd=repo)
    (repo / '.gitignore').write_text('_plans/\n__pycache__/\n')
    (repo / 'app.txt').write_text('committed\n')
    install_tools(repo)
    run_git('add', '.', cwd=repo)
    run_git('commit', '-qm', 'create fixture', cwd=repo)
    (repo / '_plans').mkdir()
    (repo / '_plans/example.md').write_text(executable_plan(count=count))
    return repo


class PreviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state'), 'FAKE_PREVIEW_MODE': ''})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def mode(self, value: str) -> None:
        os.environ['FAKE_PREVIEW_MODE'] = value


class PureRuleTests(unittest.TestCase):
    def test_slug_resolution(self) -> None:
        self.assertEqual(preview.resolve_run_slug('example', []), 'example')
        self.assertEqual(preview.resolve_run_slug('feature/Login', []), 'feature/Login')
        self.assertIsNone(preview.resolve_run_slug(None, []))
        self.assertEqual(preview.resolve_run_slug(None, ['example']), 'example')
        self.assertEqual(preview.resolve_run_slug('example', ['example', 'example']), 'example')
        for run_slug, execute, message in ((None, ['a', 'b'], 'multiple'), ('a', ['b'], 'conflicts'), ('-x', [], 'invalid'),
                                           ('a b', [], 'invalid'), ('x.md', [], 'invalid'), (None, ['Bad'], 'invalid'),
                                           (None, ['x.md'], 'invalid')):
            with self.subTest(run=run_slug, execute=execute), self.assertRaisesRegex(StateError, message):
                preview.resolve_run_slug(run_slug, execute)

    def test_host_label(self) -> None:
        self.assertEqual(preview.host_label('example-2'), 'example-2')
        self.assertEqual(preview.host_label('feature/Login_Page'), 'feature-login-page')
        self.assertEqual(preview.host_label('123-fix'), 'b-123-fix')
        long = preview.host_label('x' * 100)
        self.assertLessEqual(len(long), 63)
        self.assertRegex(long, pp.SLUG_RE)
        self.assertNotEqual(long, preview.host_label('x' * 101))
        with self.assertRaisesRegex(StateError, 'cannot derive'):
            preview.host_label('///')

    def test_plain_task_id_is_stable(self) -> None:
        self.assertEqual(preview.plain_task_id('/c', '/w'), preview.plain_task_id('/c', '/w'))
        self.assertNotEqual(preview.plain_task_id('/c', '/w'), preview.plain_task_id('/c', '/x'))
        self.assertRegex(preview.plain_task_id('/c', '/w'), pp.TASK_ID_RE)


class PlainPreviewTests(PreviewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = plain_repository(self.root)
        self.plan = self.repo / '_plans/example.md'

    def start_execution(self) -> None:
        with working_directory(self.repo):
            workflow.start('example', runner=['unused'])

    @property
    def state_dir(self) -> Path:
        return self.repo / '.worktree-runs/example/state'

    def test_unexecuted_plan_without_worktree_is_refused(self) -> None:
        with working_directory(self.repo), self.assertRaisesRegex(StateError, 'execute: example first'):
            preview.run('example', [], [])
        self.assertFalse((self.repo / '.worktree-runs').exists())
        self.assertEqual(len(preview.listed_worktrees(self.repo)), 1)

    def test_impromptu_worktree_branch_and_current_worktree(self) -> None:
        impromptu = self.root / 'impromptu'
        run_git('worktree', 'add', '-qb', 'feature/Quick', str(impromptu), cwd=self.repo)
        (impromptu / 'app.txt').write_text('impromptu edit\n')
        with working_directory(self.repo):
            result = preview.run('feature/Quick', [], [])
            self.assertEqual((result['source'], result['hostname'], result['worktree']), ('worktree', 'feature-quick.localhost', str(impromptu)))
            self.assertEqual(result['details']['source'], 'impromptu edit\n')
            self.assertTrue(result['task_id'].startswith('plain-'))
            self.assertEqual(preview.status_runtime(preview.resolve_target('feature/Quick'), timeout=30)['status'], 'running')
        self.assertTrue((impromptu / '.worktree-runs/feature-quick/runtime.json').is_file())
        self.assertEqual(workflow.git_output(impromptu, 'status', '--porcelain'), 'M app.txt')
        with working_directory(impromptu / pp.TOOLS_DIR):
            again = preview.run(None, [], [])
        self.assertEqual((again['note'], again['runtime_id']), ('reused running preview', result['runtime_id']))
        branch = workflow.git_output(self.repo, 'symbolic-ref', '--short', 'HEAD')
        with working_directory(self.repo):
            current = preview.run(None, [], [])
        self.assertEqual((current['worktree'], current['plan_slug']), (str(self.repo), preview.host_label(branch)))

    def test_unexecuted_plan_previews_matching_worktree(self) -> None:
        run_git('worktree', 'add', '-qb', 'example', str(self.root / 'example-wt'), cwd=self.repo)
        with working_directory(self.repo):
            result = preview.run('example', [], [])
        self.assertEqual((result['source'], result['worktree']), ('worktree', str(self.root / 'example-wt')))

    def test_plain_branch_without_worktree_is_not_created(self) -> None:
        run_git('branch', 'lonely', cwd=self.repo)
        with working_directory(self.repo):
            with self.assertRaisesRegex(StateError, 'git worktree add <path> lonely'):
                preview.run('lonely', [], [])
            with self.assertRaisesRegex(StateError, 'no executed plan, worktree, or local/origin branch'):
                preview.run('missing', [], [])
        self.assertEqual(len(preview.listed_worktrees(self.repo)), 1)

    def test_standalone_run_preserves_dirty_source_and_plan(self) -> None:
        self.start_execution()
        (self.repo / 'app.txt').write_text('uncommitted edit\n')
        head = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
        plan = self.plan.read_text()
        with working_directory(self.repo):
            result = preview.run('example', [], [])
        self.assertEqual(result['hostname'], 'example.localhost')
        self.assertEqual(result['services'], [{'name': 'web', 'url': 'http://example.localhost:8080/', 'port': 8080}])
        self.assertEqual(result['details']['source'], 'uncommitted edit\n')
        self.assertEqual(result['details']['db_password'], '<redacted>')
        self.assertEqual((self.repo / 'app.txt').read_text(), 'uncommitted edit\n')
        self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.plan.read_text(), plan)
        self.assertEqual(workflow.git_output(self.repo, 'status', '--porcelain'), 'M app.txt')
        argv = calls(self.state_dir, 'start')[0]['argv']
        self.assertEqual(argv[:8], ['--plan-slug', 'example', '--hostname', 'example.localhost', '--task-id', result['task_id'], '--state-dir', str(self.state_dir)])

    def test_restart_reuse_stop_reset_and_teardown(self) -> None:
        self.start_execution()
        registry = self.root / 'state/coding-skill/preview-hostnames'
        with working_directory(self.repo):
            first = preview.run('example', [], [])
            again = preview.run('example', [], [])
            self.assertEqual(again['note'], 'reused running preview')
            self.assertEqual(len(calls(self.state_dir, 'start')), 1)
            (self.state_dir / 'data').write_text('user data')
            stopped = preview.stop_runtime(preview.resolve_target('example'), teardown=False, timeout=30)
            self.assertEqual((stopped['phase'], stopped['status']), ('stopped', 'stopped'))
            self.assertEqual(preview.status_runtime(preview.resolve_target('example'), timeout=30)['status'], 'stopped')
            restarted = preview.run('example', [], [])
            self.assertEqual(restarted['details']['data'], 'user data')
            self.assertEqual(restarted['runtime_id'], first['runtime_id'])
            reset = preview.reset_runtime(preview.resolve_target('example'), None, timeout=30)
            self.assertEqual((reset['phase'], reset['details']['data']), ('running', 'fresh'))
            self.assertTrue((registry / 'example.localhost.json').exists())
            torn = preview.stop_runtime(preview.resolve_target('example'), teardown=True, timeout=30)
            self.assertEqual(torn['phase'], 'torn-down')
            self.assertFalse((self.state_dir / 'resource').exists())
            self.assertFalse((registry / 'example.localhost.json').exists())
            count = len(calls(self.state_dir))
            noop = preview.stop_runtime(preview.resolve_target('example'), teardown=True, timeout=30)
            self.assertEqual(noop['note'], 'no owned preview resources')
            self.assertEqual(len(calls(self.state_dir)), count)
            self.assertEqual(preview.run('example', [], [])['phase'], 'running')
        self.assertEqual(workflow.git_output(self.repo, 'status', '--porcelain'), '')

    def test_argument_forwarding_and_validation(self) -> None:
        self.start_execution()
        with working_directory(self.repo):
            with self.assertRaisesRegex(StateError, 'unsupported start argument'):
                preview.run('example', [], ['--bogus'])
            self.assertEqual(calls(self.state_dir), [])
            result = preview.run('example', [], ['--seed', 'a b; echo pwned'])
            self.assertEqual(result['details']['start_args'], ['--seed', 'a b; echo pwned'])
            with self.assertRaisesRegex(StateError, 'already running with start options'):
                preview.run('example', [], [])

    def test_readiness_failure_and_interrupted_startup_retain_records(self) -> None:
        self.start_execution()
        with working_directory(self.repo):
            self.mode('not-ready')
            with self.assertRaisesRegex(StateError, 'not ready'):
                preview.run('example', [], [])
            record = json.loads((self.repo / '.worktree-runs/example/runtime.json').read_text())
            self.assertEqual(record['phase'], 'failed')
            self.assertTrue((self.state_dir / 'resource').exists())
            self.mode('start,crash')
            with self.assertRaisesRegex(StateError, 'without a valid JSON envelope') as caught:
                preview.run('example', [], [])
            self.assertNotIn('hunter2', str(caught.exception))
            self.mode('')
            real_invoke = preview.invoke
            def interrupted(target: preview.Target, operation: str, *extra: str, **kwargs: object) -> preview.Invocation:
                raise KeyboardInterrupt
            with patch('preview.invoke', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
                preview.run('example', [], [])
            self.assertEqual(json.loads((self.repo / '.worktree-runs/example/runtime.json').read_text())['phase'], 'starting')
            self.assertFalse((self.repo / '.worktree-runs/example/runtime.lock').exists())
            self.assertIs(preview.invoke, real_invoke)
            self.assertEqual(preview.run('example', [], [])['phase'], 'running')

    def test_failed_stop_blocks_start_until_recovered(self) -> None:
        self.start_execution()
        with working_directory(self.repo):
            preview.run('example', [], [])
            self.mode('stop-fail')
            with self.assertRaisesRegex(StateError, 'teardown failed'):
                preview.stop_runtime(preview.resolve_target('example'), teardown=True, timeout=30)
            self.mode('')
            with self.assertRaisesRegex(StateError, 'interrupted tearing-down'):
                preview.run('example', [], [])
            with self.assertRaisesRegex(StateError, 'interrupted teardown'):
                preview.stop_runtime(preview.resolve_target('example'), teardown=False, timeout=30)
            self.assertEqual(preview.stop_runtime(preview.resolve_target('example'), teardown=True, timeout=30)['phase'], 'torn-down')

    def test_missing_tools_and_unsupported_repository(self) -> None:
        self.start_execution()
        with working_directory(self.repo):
            (self.repo / pp.TOOLS_DIR / 'logs').unlink()
            with self.assertRaisesRegex(StateError, 'worktree-tools/logs'):
                preview.run('example', [], [])
            run_git('checkout', '--', '.', cwd=self.repo)
            (self.repo / pp.TOOLS_DIR / pp.PROTOCOL_FILE).write_text(json.dumps({**PROTOCOL, 'schema_version': 2}))
            with self.assertRaisesRegex(StateError, 'incompatible'):
                preview.run('example', [], [])

    def test_independent_repository_cannot_share_hostname(self) -> None:
        self.start_execution()
        (self.root / 'second').mkdir()
        other = plain_repository(self.root / 'second')
        with working_directory(other):
            workflow.start('example', runner=['unused'])
        with working_directory(self.repo):
            preview.run('example', [], [])
        with working_directory(other), self.assertRaisesRegex(StateError, 'independent runtime'):
            preview.run('example', [], [])
        self.assertEqual(calls(other / '.worktree-runs/example/state'), [])

    def test_combined_execute_run_waits_for_audit_and_discharges_once(self) -> None:
        from completion import complete, report
        with working_directory(self.repo):
            intent = preview.record_intent(preview.resolve_run_slug(None, ['example']), ['--seed', 'x'])
            self.assertEqual(intent['phase'], 'pending')
            self.assertEqual(preview.record_intent('example', [])['start_args'], ['--seed', 'x'])
            with self.assertRaisesRegex(StateError, 'different start options'):
                preview.record_intent('example', ['--seed', 'y'])
            workflow.start('example', runner=['unused'])
            workflow.begin('example')
            with self.assertRaises(StateError):
                preview.run(None, ['example'], [])
            self.assertEqual(calls(self.state_dir), [])
            (self.repo / 'change.txt').write_text('task\n')
            complete('example')
            self.assertEqual(json.loads(report('example'))['run_intent'], 'pending')
            result = preview.run(None, ['example'], [])
            self.assertEqual((result['intent'], result['details']['start_args']), ('discharged', ['--seed', 'x']))
            retried = preview.run(None, ['example'], [])
            self.assertEqual(retried['note'], 'reused running preview')
            self.assertEqual(len(calls(self.state_dir, 'start')), 1)
            self.assertIn('Run intent: discharged', report('example', markdown=True))
            from completion import audit
            audit('example')

    def test_cli_forwards_arguments_and_bounds_logs(self) -> None:
        self.start_execution()
        script = [sys.executable, str(SCRIPTS / 'preview.py')]
        env = {**os.environ, 'PYTHONPATH': ''}
        result = subprocess.run([*script, 'run', 'example', '--', '--seed', 'cli value'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['details']['start_args'], ['--seed', 'cli value'])
        logs = subprocess.run([*script, 'logs', 'example', '--lines', '3'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertEqual(logs.stdout.splitlines(), ['line 3', 'line 4', 'line 5'])
        bad = subprocess.run([*script, 'status', 'example', '--', '--seed', 'x'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn('does not accept start options', bad.stderr)
        status = subprocess.run([*script, 'status', 'example'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)['status'], 'running')


class FaurPreviewTests(PreviewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace, self.launch = RepositoryFixture(self.root).faur()
        run_git('config', 'user.name', 'Preview Tests', cwd=self.launch)
        run_git('config', 'user.email', 'test@example.invalid', cwd=self.launch)
        (self.launch / 'app.txt').write_text('committed\n')
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
        self.context = {'schema_version': 1, 'command': 'context', 'ok': True, 'workspace': {'common_dir': common}, 'worktree': launch_tree, 'task': {'status': 'none', 'record': None}}
        record = {'schema_version': 1, 'task_id': 'immutable-preview-id', 'workspace': {'common_dir': common}, 'requested_slug': 'example', 'phase': 'ready', 'launch': launch_tree, 'task': {'name': 'example-2', 'path': str(self.task), 'branch': 'example-2'}}
        worktree = {'name': 'example-2', 'path': str(self.task), 'branch': 'example-2', 'head': head}
        self.created = {'schema_version': 1, 'command': 'add', 'ok': True, 'dry_run': False, 'worktree': worktree, 'task_id': record['task_id'], 'task': record, 'launch': launch_tree}
        self.faur = patch('workflow.run_faur', side_effect=self.fake_faur)
        self.faur.start()
        with working_directory(self.launch):
            workflow.start('example', runner=['fixture'])

    def tearDown(self) -> None:
        self.faur.stop()
        super().tearDown()

    def fake_faur(self, runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
        if args[0] == 'add':
            return {**self.created, 'dry_run': '--dry-run' in args}
        if cwd == self.launch and len(args) == 2:
            return self.context
        return {**self.context, 'worktree': self.created['worktree'], 'task': {'status': 'valid', 'record': self.created['task']}}

    def test_suffixed_worktree_keeps_plan_hostname_and_runs_from_main(self) -> None:
        with working_directory(self.launch):
            result = preview.run('example', [], [])
        self.assertEqual(result['hostname'], 'example.localhost')
        self.assertEqual(result['worktree'], str(self.task))
        self.assertEqual(result['task_id'], 'immutable-preview-id')
        self.assertTrue((self.workspace / '_runs/example/runtime.json').is_file())
        self.assertEqual(workflow.git_output(self.task, 'status', '--porcelain'), '')
        self.assertEqual(workflow.git_output(self.launch, 'status', '--porcelain'), '')
        with working_directory(self.task):
            self.assertEqual(preview.run('example', [], [])['note'], 'reused running preview')

    def test_changed_faur_identity_is_refused(self) -> None:
        self.created['task'] = {**self.created['task'], 'task_id': 'someone-else'}
        with working_directory(self.launch), self.assertRaisesRegex(StateError, 'identity changed'):
            preview.run('example', [], [])
        self.assertFalse((self.workspace / '_runs').exists())



class FaurAdHocPreviewTests(PreviewTestCase):
    """Worktrees and branches without plans, validated through a fake faur."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace, self.launch = RepositoryFixture(self.root).faur()
        run_git('config', 'user.name', 'Preview Tests', cwd=self.launch)
        run_git('config', 'user.email', 'test@example.invalid', cwd=self.launch)
        (self.launch / '.gitignore').write_text('__pycache__/\n')
        install_tools(self.launch)
        run_git('add', '.', cwd=self.launch)
        run_git('commit', '-qm', 'add preview tools', cwd=self.launch)
        run_git('remote', 'remove', 'origin', cwd=self.launch)
        self.common = str(self.workspace / '.bare')
        self.tasks: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.faur = patch('workflow.run_faur', side_effect=self.fake_faur)
        self.faur.start()

    def tearDown(self) -> None:
        self.faur.stop()
        super().tearDown()

    def fake_faur(self, runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
        self.calls.append(args)
        if args[0] == 'add':
            path = self.workspace / args[1]
            run_git('worktree', 'add', '-q', str(path), args[1], cwd=self.launch)
            return {'schema_version': 1, 'command': 'add', 'ok': True, 'dry_run': False, 'task_id': None, 'task': None,
                    'worktree': {'name': args[1], 'path': str(path), 'branch': args[1], 'head': None}}
        root = Path(workflow.git_output(cwd, 'rev-parse', '--show-toplevel')).resolve()
        name = root.relative_to(self.workspace).as_posix()
        task_id = self.tasks.get(name)
        task = {'status': 'valid', 'record': {'task_id': task_id}} if task_id else {'status': 'none', 'record': None}
        return {'schema_version': 1, 'command': 'context', 'ok': True, 'workspace': {'common_dir': self.common},
                'worktree': {'name': name, 'path': str(root), 'branch': workflow.git_output(root, 'symbolic-ref', '--short', 'HEAD')}, 'task': task}

    def test_impromptu_task_worktree_uses_faur_task_id(self) -> None:
        run_git('worktree', 'add', '-qb', 'quick-fix', str(self.workspace / 'quick-fix'), cwd=self.launch)
        self.tasks['quick-fix'] = 'impromptu-task-id'
        with working_directory(self.launch):
            result = preview.run('quick-fix', [], [])
        self.assertEqual((result['source'], result['task_id'], result['hostname']), ('worktree', 'impromptu-task-id', 'quick-fix.localhost'))
        self.assertTrue((self.workspace / '_runs/quick-fix/runtime.json').is_file())
        self.assertNotIn('add', [c[0] for c in self.calls])

    def test_branch_without_worktree_gets_one_from_faur(self) -> None:
        run_git('branch', 'feature/Login', cwd=self.launch)
        with working_directory(self.launch):
            with self.assertRaisesRegex(StateError, 'run creates one'):
                preview.status_runtime(preview.resolve_target('feature/Login'), timeout=30)
            self.assertEqual(self.calls, [])
            result = preview.run('feature/Login', [], [])
            created = self.workspace / 'feature/Login'
            self.assertEqual((result['source'], result['worktree'], result['hostname']), ('created-worktree', str(created), 'feature-login.localhost'))
            self.assertTrue(result['task_id'].startswith('worktree-'))
            self.assertEqual(self.calls[0], ('add', 'feature/Login', '--json'))
            again = preview.run('feature/Login', [], [])
            self.assertEqual((again['source'], again['note']), ('worktree', 'reused running preview'))
            self.assertEqual(len([c for c in self.calls if c[0] == 'add']), 1)
            torn = preview.stop_runtime(preview.resolve_target('feature/Login'), teardown=True, timeout=30)
            self.assertEqual(torn['phase'], 'torn-down')

    def test_unknown_name_creates_nothing(self) -> None:
        with working_directory(self.launch), self.assertRaisesRegex(StateError, 'no executed plan, worktree, or local/origin branch'):
            preview.run('nothing-here', [], [])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.workspace / '_runs').exists())


if __name__ == '__main__':
    unittest.main()
