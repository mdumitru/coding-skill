"""Disposable Git fixtures and contract tests for the execution runner."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from unittest.mock import patch

from tests.test_plan_file import RepositoryFixture, run_git

SCRIPTS = Path(__file__).parents[1] / 'skills/task-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import workflow
from state_io import StateError


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def executable_plan(*, faur: bool = False, count: int = 2) -> str:
    prefix = '# TODO: fixture\n' + ('Worktree: example\n' if faur else '')
    return prefix + ''.join(f'- [ ] Task {n}\n  - Task: task-{n}\n  - Commit: implement task {n}\n  - Paths: ["*.txt"]\n  - Verify: ["{sys.executable}", "-c", "print(123)"]\n  - Done when: acceptance holds\n' for n in range(1, count + 1))


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = RepositoryFixture(self.root).plain()
        run_git('config', 'user.name', 'Tests', cwd=self.repo)
        run_git('config', 'user.email', 'test@example.invalid', cwd=self.repo)
        (self.repo / '.gitignore').write_text('_plans/\n')
        (self.repo / 'initial.txt').write_text('initial\n')
        run_git('add', '.', cwd=self.repo)
        run_git('commit', '-qm', 'create fixture', cwd=self.repo)
        self.plan = self.repo / '_plans/example.md'
        self.plan.parent.mkdir()
        self.plan.write_text(executable_plan())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plain_start_begin_and_safe_repeat(self) -> None:
        with working_directory(self.repo):
            state = workflow.start('example', runner=['unused'])
            self.assertEqual(state.identity.path, str(self.repo))
            begun = workflow.begin('example')
            self.assertEqual(begun.active.task_id, 'task-1')
            self.assertEqual(workflow.begin('example').active, begun.active)
            self.assertEqual(workflow.start('example', runner=['unused']).active, begun.active)
        self.assertFalse((self.plan.parent / '.state/example.lock').exists())

    def test_dirty_wrong_cwd_and_branch_refusal(self) -> None:
        with working_directory(self.repo):
            workflow.start('example', runner=['unused'])
            (self.repo / 'other.txt').write_text('unrelated')
            with self.assertRaisesRegex(StateError, 'clean'):
                workflow.begin('example')
            (self.repo / 'other.txt').unlink()
            run_git('checkout', '-qb', 'wrong', cwd=self.repo)
            with self.assertRaisesRegex(StateError, 'identity changed'):
                workflow.begin('example')
        # A different worktree with the same common directory cannot act on this one.
        linked = self.root / 'linked'
        run_git('worktree', 'add', '-qb', 'linked', str(linked), cwd=self.repo)
        context = workflow.git_context(self.repo)
        with working_directory(linked), self.assertRaisesRegex(StateError, 'recorded worktree'):
            workflow.check_identity(workflow.load(self.plan), context)

    def test_plan_drift_and_second_active_task_refused(self) -> None:
        with working_directory(self.repo):
            state = workflow.start('example', runner=['unused'])
            state.active = workflow.ActiveTask('task-2', workflow.git_output(self.repo, 'rev-parse', 'HEAD'))
            workflow.save(self.plan, state)
            with self.assertRaisesRegex(StateError, 'another task'):
                workflow.begin('example')
            self.plan.write_text(self.plan.read_text() + '\nDrift\n')
            with self.assertRaisesRegex(StateError, 'drift'):
                workflow.begin('example')

    def test_cli_new_process_resume(self) -> None:
        runner = [sys.executable, str(SCRIPTS / 'workflow.py')]
        result = subprocess.run([*runner, 'start', 'example'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([*runner, 'begin', 'example'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['active']['task_id'], 'task-1')

    def test_old_schema_and_missing_faur_are_actionable(self) -> None:
        with patch('workflow.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"schema_version": 2, "ok": true}', '')):
            with self.assertRaisesRegex(StateError, 'schema'):
                workflow.run_faur(['faur'], self.repo, 'context', '--json')
        with self.assertRaisesRegex(StateError, 'unavailable'):
            workflow.run_faur(['/nonexistent/faur'], self.repo, 'context', '--json')

    def faur_fixture(self) -> tuple[Path, dict[str, object], dict[str, object]]:
        workspace, launch = RepositoryFixture(self.root).faur()
        plan = workspace / '_plans/example.md'
        plan.parent.mkdir()
        plan.write_text(executable_plan(faur=True))
        common = str(workspace / '.bare')
        branch = workflow.git_output(launch, 'branch', '--show-current')
        head = workflow.git_output(launch, 'rev-parse', 'HEAD')
        launch_data = {'schema_version': 1, 'command': 'context', 'ok': True, 'workspace': {'common_dir': common}, 'worktree': {'path': str(launch), 'branch': branch, 'name': 'main', 'head': head}, 'task': {'status': 'none', 'record': None}}
        task = workspace / 'example-2'
        run_git('worktree', 'add', '-qb', 'example-2', str(task), cwd=launch)
        record = {'schema_version': 1, 'task_id': 'immutable-fixture-id', 'workspace': {'common_dir': common}, 'requested_slug': 'example', 'phase': 'ready', 'launch': launch_data['worktree'], 'task': {'name': 'example-2', 'path': str(task), 'branch': 'example-2'}}
        created = {'schema_version': 1, 'command': 'add', 'ok': True, 'dry_run': False, 'worktree': {'name': 'example-2', 'path': str(task), 'branch': 'example-2', 'head': head}, 'task_id': record['task_id'], 'task': record, 'launch': record['launch']}
        return launch, launch_data, created

    def test_faur_contract_creation_and_plan_update_retry(self) -> None:
        launch, context, created = self.faur_fixture()
        calls: list[tuple[str, ...]] = []
        def fake(runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
            calls.append(args)
            if args[0] == 'add':
                return {**created, 'dry_run': '--dry-run' in args}
            if cwd == launch:
                return context
            return {**context, 'worktree': created['worktree'], 'task': {'status': 'valid', 'record': created['task']}}
        with working_directory(launch), patch('workflow.run_faur', side_effect=fake):
            with patch('workflow.replace_plan', side_effect=OSError('simulated replacement failure')):
                with self.assertRaises(OSError):
                    workflow.start('example', runner=['fixture'])
            state = workflow.start('example', runner=['fixture'])
            self.assertEqual(state.identity.task_id, 'immutable-fixture-id')
            self.assertEqual(sum(a[0] == 'add' and '--dry-run' not in a for a in calls), 1)
            plan = launch.parent / '_plans/example.md'
            self.assertIn('Worktree: `example-2`', plan.read_text())
        with working_directory(Path(state.identity.path)), patch('workflow.run_faur', side_effect=fake):
            self.assertEqual(workflow.begin('example').active.task_id, 'task-1')

    def test_explicit_reuse_calls_context_without_add(self) -> None:
        launch, context, created = self.faur_fixture()
        def fake(runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
            self.assertEqual(args[0], 'context')
            if len(args) == 2 and cwd == launch:
                return context
            return {**context, 'worktree': created['worktree'], 'task': {'status': 'valid', 'record': created['task']}}
        with working_directory(launch), patch('workflow.run_faur', side_effect=fake):
            state = workflow.start('example', runner=['fixture'], existing='example-2')
            self.assertEqual(state.identity.task_id, 'immutable-fixture-id')

    def test_unacknowledged_creation_never_creates_second_task(self) -> None:
        launch, context, created = self.faur_fixture()
        def fake(runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
            if args[0] == 'context':
                if len(args) == 2 and cwd == launch:
                    return context
                return {**context, 'worktree': created['worktree'], 'task': {'status': 'valid', 'record': created['task']}}
            if '--dry-run' in args:
                return {**created, 'dry_run': True}
            raise StateError('response lost after creation')
        with working_directory(launch), patch('workflow.run_faur', side_effect=fake):
            with self.assertRaisesRegex(StateError, 'response lost'):
                workflow.start('example', runner=['fixture'])
            with self.assertRaisesRegex(StateError, 'no second task'):
                workflow.start('example', runner=['fixture'])
            with self.assertRaisesRegex(StateError, 'task ID'):
                workflow.start('example', runner=['fixture'], recover_task_id='wrong')
            state = workflow.start('example', runner=['fixture'], recover_task_id='immutable-fixture-id')
            self.assertEqual(state.phase, 'ready')


class RealFaurStartupTests(unittest.TestCase):
    def test_real_unique_creation_and_new_process_resume(self) -> None:
        raw = os.environ.get('FAUR_WORKTREE_ARGV')
        if not raw:
            self.skipTest('set FAUR_WORKTREE_ARGV to the companion CLI JSON argv')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, launch = RepositoryFixture(root).faur()
            (workspace / '.bare/info/exclude').write_text('_plans/\n')
            plan = workspace / '_plans/example.md'
            plan.parent.mkdir()
            plan.write_text(executable_plan(faur=True))
            run_git('branch', 'example', cwd=launch)
            runner = [sys.executable, str(SCRIPTS / 'workflow.py')]
            created = subprocess.run([*runner, 'start', 'example', '--faur-argv', raw], cwd=launch, capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            state = json.loads(created.stdout)
            self.assertEqual(state['identity']['name'], 'example-2')
            self.assertTrue(state['identity']['task_id'])
            task_path = Path(state['identity']['path'])
            resumed = subprocess.run([*runner, 'start', 'example', '--faur-argv', raw], cwd=task_path, capture_output=True, text=True)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)['identity'], state['identity'])
            begun = subprocess.run([*runner, 'begin', 'example'], cwd=task_path, capture_output=True, text=True)
            self.assertEqual(begun.returncode, 0, begun.stderr)
            self.assertEqual(json.loads(begun.stdout)['active']['task_id'], 'task-1')

class CompletionTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown

    def start_begin(self) -> None:
        workflow.start('example', runner=['unused'])
        workflow.begin('example')

    def test_ordered_completion_no_change_audit_and_report(self) -> None:
        from completion import complete, audit, report
        with working_directory(self.repo):
            self.start_begin()
            (self.repo / 'change.txt').write_text('task one\n')
            state = complete('example')
            self.assertEqual(len(state.records), 1)
            first_head = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
            workflow.begin('example')
            state = complete('example')
            self.assertEqual(state.records[1]['outcome'], 'no-change')
            self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), first_head)
            self.assertEqual(audit('example').records, state.records)
            facts = json.loads(report('example'))
            self.assertFalse(facts['dirty'])
            self.assertEqual(facts['pending'], [])
            self.assertIn(first_head[:7], report('example', markdown=True))

    def test_verification_failure_and_mutation_leave_task_pending(self) -> None:
        from completion import complete
        for command in ('raise SystemExit(3)', 'from pathlib import Path; Path("mutation.txt").write_text("mutation")'):
            with self.subTest(command=command), working_directory(self.repo):
                # Reset only the disposable execution journal between fixture variants.
                state_file = self.plan.parent / '.state/example.json'
                state_file.unlink(missing_ok=True)
                self.plan.write_text(executable_plan(count=1).replace('print(123)', command.replace('"', '\\"')))
                self.start_begin()
                (self.repo / 'change.txt').write_text('change')
                with self.assertRaisesRegex(StateError, 'verification'):
                    complete('example')
                state = workflow.load(self.plan)
                self.assertIsNotNone(state.active.error)
                self.assertIn('- [ ]', self.plan.read_text())
                (self.repo / 'mutation.txt').unlink(missing_ok=True)
                (self.repo / 'change.txt').unlink()

    def test_unauthorized_deletion_rename_partial_staging_and_forbidden_paths(self) -> None:
        from completion import complete
        with working_directory(self.repo):
            self.start_begin()
            (self.repo / 'unrelated.py').write_text('bad')
            with self.assertRaisesRegex(StateError, 'unauthorized'):
                complete('example')
            (self.repo / 'unrelated.py').unlink()
            (self.repo / 'initial.txt').write_text('staged')
            run_git('add', 'initial.txt', cwd=self.repo)
            (self.repo / 'initial.txt').write_text('partial')
            with self.assertRaisesRegex(StateError, 'partial staging'):
                complete('example')
            run_git('reset', '--hard', '-q', 'HEAD', cwd=self.repo)
            run_git('mv', 'initial.txt', 'renamed.txt', cwd=self.repo)
            state = complete('example')
            self.assertTrue(state.records[0]['outcome'])
            self.assertFalse((self.repo / 'initial.txt').exists())
            self.assertTrue((self.repo / 'renamed.txt').exists())
            workflow.begin('example')
            (self.repo / 'TODO-work.md').write_text('tracking')
            run_git('add', '-f', 'TODO-work.md', cwd=self.repo)
            with self.assertRaisesRegex(StateError, 'unauthorized'):
                complete('example')

    def test_failed_commit_retains_staging_and_recovers_once(self) -> None:
        from completion import complete
        with working_directory(self.repo):
            self.start_begin()
            hook = self.repo / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nexit 1\n')
            hook.chmod(0o755)
            (self.repo / 'change.txt').write_text('task\n')
            with self.assertRaisesRegex(StateError, 'commit failed'):
                complete('example')
            self.assertEqual(workflow.load(self.plan).active.phase, 'committing')
            self.assertTrue(workflow.git_output(self.repo, 'diff', '--cached', '--name-only'))
            hook.unlink()
            state = complete('example')
            self.assertEqual(len(state.records), 1)
            self.assertIsNone(state.active)

    def test_crash_after_git_commit_and_after_markdown_replacement(self) -> None:
        import completion
        with working_directory(self.repo):
            self.start_begin()
            (self.repo / 'change.txt').write_text('task\n')
            real_save = workflow.save
            def crash(plan: Path, state: workflow.Execution) -> None:
                if state.active and state.active.phase == 'committed':
                    raise OSError('crash after Git commit')
                real_save(plan, state)
            with patch('workflow.save', side_effect=crash), self.assertRaises(OSError):
                completion.complete('example')
            committed = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
            state = completion.complete('example')
            self.assertEqual(state.records[0]['outcome'], committed)
            self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), committed)
            workflow.begin('example')
            # No-change task: fail the final state write after Markdown succeeds.
            def final_crash(plan: Path, state: workflow.Execution) -> None:
                if state.active is None:
                    raise OSError('crash after Markdown update')
                real_save(plan, state)
            with patch('workflow.save', side_effect=final_crash), self.assertRaises(OSError):
                completion.complete('example')
            state = completion.complete('example')
            self.assertEqual(state.records[1]['outcome'], 'no-change')
            completion.audit('example')

    def test_plan_drift_and_rewritten_history_fail_audit(self) -> None:
        from completion import complete, audit
        with working_directory(self.repo):
            self.plan.write_text(executable_plan(count=1))
            self.start_begin()
            (self.repo / 'change.txt').write_text('task\n')
            complete('example')
            content = self.plan.read_text()
            self.plan.write_text(content + '\nDrift\n')
            with self.assertRaisesRegex(StateError, 'drift'):
                audit('example')
            self.plan.write_text(content)
            run_git('reset', '--hard', '-q', 'HEAD^', cwd=self.repo)
            with self.assertRaisesRegex(StateError, 'ancestry'):
                audit('example')

    def test_checkboxes_without_evidence_never_start_or_audit(self) -> None:
        with working_directory(self.repo):
            self.plan.write_text(executable_plan(count=1).replace('- [ ]', '- [x]'))
            with self.assertRaisesRegex(StateError, 'Evidence'):
                workflow.start('example', runner=['unused'])

    def test_unauthorized_verification_mutation_is_preserved(self) -> None:
        from completion import complete
        with working_directory(self.repo):
            self.plan.write_text(executable_plan(count=1).replace('print(123)', 'from pathlib import Path; Path(\"bad.py\").touch()'.replace('"', '\\"')))
            self.start_begin()
            with self.assertRaisesRegex(StateError, 'unauthorized'):
                complete('example')
            self.assertTrue((self.repo / 'bad.py').exists())
            self.assertFalse(workflow.read_plan(self.plan, workflow.git_context()).tasks[0].completed)
