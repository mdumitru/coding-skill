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
from plan_file import PlanFileError
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

    def test_failed_commit_can_reverify_a_correction_on_the_same_paths(self) -> None:
        from completion import complete
        with working_directory(self.repo):
            self.plan.write_text(
                executable_plan(count=1).replace(
                    'print(123)',
                    'print(open(\\"change.txt\\").read().strip())',
                )
            )
            self.start_begin()
            hook = self.repo / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\ngrep -qx good change.txt\n')
            hook.chmod(0o755)
            changed = self.repo / 'change.txt'
            changed.write_text('bad\n')
            with self.assertRaisesRegex(StateError, 'commit failed'):
                complete('example')
            failed = workflow.load(self.plan)
            self.assertTrue(failed.active.commit_failed)
            failed_tree = failed.active.tree

            # Editing a staged path leaves a partial index/worktree state. Recovery
            # must reverify and restage it instead of requiring the failed tree.
            changed.write_text('good\n')
            state = complete('example')

            self.assertEqual(len(state.records), 1)
            self.assertNotEqual(state.records[0]['tree'], failed_tree)
            self.assertEqual(state.records[0]['evidence'][0]['stdout'], 'good\n')
            self.assertEqual(
                workflow.git_output(self.repo, 'show', 'HEAD:change.txt'), 'good'
            )

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

class ProducerConsumerLifecycleTests(unittest.TestCase):
    def test_real_complete_audit_new_process_finish_and_retained_evidence(self) -> None:
        raw = os.environ.get('FAUR_WORKTREE_ARGV')
        if not raw:
            self.skipTest('set FAUR_WORKTREE_ARGV to enable the real companion lifecycle')
        faur = json.loads(raw)
        with tempfile.TemporaryDirectory() as temporary:
            workspace, launch = RepositoryFixture(Path(temporary)).faur()
            (workspace / '.bare/info/exclude').write_text('_plans/\n')
            run_git('config', 'user.name', 'Lifecycle Tests', cwd=launch)
            run_git('config', 'user.email', 'test@example.invalid', cwd=launch)
            plan = workspace / '_plans/example.md'
            plan.parent.mkdir()
            plan.write_text(executable_plan(faur=True, count=1))
            runner = [sys.executable, str(SCRIPTS / 'workflow.py')]
            def command(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
                result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return result
            created = json.loads(command([*runner, 'start', 'example', '--faur-argv', raw], launch).stdout)
            task = Path(created['identity']['path'])
            task_id = created['identity']['task_id']
            command([*runner, 'begin', 'example'], task)
            (task / 'result.txt').write_text('verified task result\n')
            completed = json.loads(command([*runner, 'complete', 'example'], task).stdout)
            commit = completed['records'][0]['outcome']
            command([*runner, 'audit', 'example'], task)
            resumed = json.loads(command([*runner, 'start', 'example', '--faur-argv', raw], task).stdout)
            self.assertEqual(resumed['records'][0]['outcome'], commit)
            report = json.loads(command([*runner, 'report', 'example'], task).stdout)
            self.assertEqual(report['identity']['task_id'], task_id)
            third = workspace / 'third'
            run_git('worktree', 'add', '-qb', 'third', str(third), cwd=launch)
            command([*faur, 'finish', 'example', '--dry-run'], third)
            self.assertTrue(task.is_dir())
            command([*faur, 'finish', 'example'], third)
            self.assertFalse(task.exists())
            self.assertEqual((launch / 'result.txt').read_text(), 'verified task result\n')
            self.assertNotEqual(subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/example'], cwd=launch).returncode, 0)
            self.assertIn(commit, plan.read_text())
            self.assertIn('"exit_code": 0', plan.read_text())
            archived = list((workspace / '.bare/faur/task-state').rglob(f'{task_id}.json'))
            self.assertEqual(len(archived), 1)
            record = json.loads(archived[0].read_text())
            self.assertEqual(record['launch']['path'], str(launch))
            self.assertTrue(record['branch_deleted'])

    def test_installed_runner_lifecycles_in_both_harnesses(self) -> None:
        from tests.test_installer import IsolatedHome
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = IsolatedHome(root / 'home')
            home.root.mkdir()
            home.initialize_targets()
            home.run('--dry-run')
            home.run()
            home.run('--check')
            for target in (home.claude, home.codex):
                repo = root / target.name
                repo.mkdir()
                run_git('init', '-q', cwd=repo)
                run_git('config', 'user.name', 'Copied Helpers', cwd=repo)
                run_git('config', 'user.email', 'test@example.invalid', cwd=repo)
                (repo / '.gitignore').write_text('_plans/\n')
                run_git('add', '.', cwd=repo)
                run_git('commit', '-qm', 'create fixture', cwd=repo)
                scripts = target / 'skills/task-workflow/scripts'
                content = root / 'content.md'
                content.write_text(executable_plan(count=1))
                commands = [[str(scripts / 'plan_file.py'), 'init', 'example', '--content', str(content)],
                            [str(scripts / 'workflow.py'), 'start', 'example'],
                            [str(scripts / 'workflow.py'), 'begin', 'example']]
                for argv in commands:
                    result = subprocess.run([sys.executable, *argv], cwd=repo, env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                (repo / 'result.txt').write_text('copied workflow\n')
                for operation in ('complete', 'audit', 'report'):
                    result = subprocess.run([sys.executable, str(scripts / 'workflow.py'), operation, 'example'], cwd=repo, env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
            before = set(home.backups.glob('*'))
            second = home.run()
            self.assertIn('up to date', second.stdout)
            self.assertEqual(set(home.backups.glob('*')), before)
            home.run('--check')
            home.run('--uninstall', '--dry-run')
            home.run('--uninstall')

class FinalRecoveryTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown
    start_begin = CompletionTests.start_begin

    def test_approved_revision_preserves_progress_and_recovers_atomically(self) -> None:
        with working_directory(self.repo):
            self.start_begin()
            source = self.root / 'revision.md'
            source.write_text(self.plan.read_text().replace('acceptance holds', 'approved acceptance holds') + '\nApproved decision: retain the existing format.\n')
            with patch('workflow.replace_plan', side_effect=OSError('replacement interrupted')):
                with self.assertRaises(OSError):
                    workflow.revise('example', source)
            state = workflow.revise('example', None)
            self.assertEqual(state.phase, 'ready')
            self.assertEqual(state.active.task_id, 'task-1')
            self.assertIn('Approved decision', self.plan.read_text())
            source.write_text(self.plan.read_text().replace('- [ ]', '- [x]', 1))
            with self.assertRaisesRegex(StateError, 'checked state'):
                workflow.revise('example', source)

    def test_revision_cannot_change_completed_task_records(self) -> None:
        from completion import complete
        with working_directory(self.repo):
            self.start_begin()
            complete('example')
            source = self.root / 'revision.md'
            source.write_text(self.plan.read_text().replace('implement task 1', 'rewrite task 1'))
            with self.assertRaisesRegex(StateError, 'completed task'):
                workflow.revise('example', source)

    def test_staging_race_preserves_changed_work_without_committing(self) -> None:
        import completion
        with working_directory(self.repo):
            self.start_begin()
            file = self.repo / 'change.txt'
            file.write_text('verified content\n')
            initial = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
            original_run = subprocess.run
            def racing_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ['git', 'add', '--all']:
                    file.write_text('concurrent edit\n')
                return original_run(argv, **kwargs)
            with patch('completion.subprocess.run', side_effect=racing_run):
                with self.assertRaisesRegex(StateError, 'drifted'):
                    completion.complete('example')
            self.assertEqual(file.read_text(), 'concurrent edit\n')
            self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), initial)

    def test_committed_transaction_retries_failed_plan_replacement_once(self) -> None:
        import completion
        with working_directory(self.repo):
            self.start_begin()
            (self.repo / 'change.txt').write_text('task\n')
            with patch('completion.replace_plan', side_effect=OSError('plan replacement failed')):
                with self.assertRaises(OSError):
                    completion.complete('example')
            committed = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
            state = completion.complete('example')
            self.assertEqual(state.records[0]['outcome'], committed)
            self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), committed)

    def test_corrupt_execution_state_is_actionable(self) -> None:
        with working_directory(self.repo):
            self.start_begin()
            path = workflow.state_path(self.plan)
            data = json.loads(path.read_text())
            data['active']['phase'] = 'unknown'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(StateError, 'unknown active'):
                workflow.load(self.plan)


def grouped_plan(*, faur: bool = False, groups: int = 2, children: int = 2) -> str:
    prefix = '# TODO: grouped fixture\n' + ('Worktree: example\n' if faur else '')
    prefix += '\n## Context and decisions\n\nUse plain text files to persist results.\n\n'
    for group in range(1, groups + 1):
        prefix += (f'- [ ] Group {group}\n  - Group: group-{group}\n'
                   f'  - Verify: {json.dumps([sys.executable, "-c", "print(123)"])}\n'
                   '  - Done when: child results work together.\n')
        for child in range(1, children + 1):
            prefix += (f'  - [ ] Child {group}/{child}\n    - Task: child-{group}-{child}\n'
                       f'    - Commit: implement child {group} {child}\n    - Paths: ["*.txt"]\n'
                       f'    - Verify: {json.dumps([sys.executable, "-c", "print(123)"])}\n'
                       '    - Done when: child acceptance holds.\n')
    return prefix


class GroupTransactionTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown

    def assign(self) -> str:
        self.plan.write_text(grouped_plan())
        workflow.start('example', runner=['unused'])
        return workflow.group_begin('example', 'group-1').active_group.assignment

    def complete_children(self, group: str, token: str, *, changed: bool = False) -> None:
        from completion import complete
        for n in range(1, 3):
            workflow.begin('example', group_id=group, assignment=token)
            if changed:
                (self.repo / f'{group}-{n}.txt').write_text(f'{group}/{n}\n')
            complete('example', group_id=group, assignment=token)

    def checkpoint(self, group: str, token: str) -> workflow.Execution:
        workflow.group_release('example', group, token)
        return workflow.group_complete('example', group, token, accepted=True)

    def test_two_groups_contiguous_commits_scope_and_final_audit(self) -> None:
        import completion
        with working_directory(self.repo):
            token = self.assign()
            selection = workflow.group_next('example')
            self.assertEqual(selection['group']['id'], 'group-1')
            self.assertEqual(workflow.group_begin('example', 'group-1').active_group.assignment, token)
            with self.assertRaisesRegex(StateError, 'assignment'):
                workflow.begin('example')
            with self.assertRaisesRegex(StateError, 'stale'):
                workflow.begin('example', group_id='group-1', assignment='wrong')
            with self.assertRaisesRegex(StateError, 'next pending'):
                workflow.group_begin('example', 'group-2')
            self.complete_children('group-1', token, changed=True)
            with self.assertRaisesRegex(StateError, 'boundary'):
                workflow.begin('example', group_id='group-1', assignment=token)
            with self.assertRaisesRegex(StateError, 'group-release'):
                workflow.group_complete('example', 'group-1', token, accepted=True)
            workflow.group_release('example', 'group-1', token)
            with self.assertRaisesRegex(StateError, 'accepted'):
                workflow.group_complete('example', 'group-1', token, accepted=False)
            with self.assertRaisesRegex(StateError, 'released'):
                workflow.begin('example', group_id='group-1', assignment=token)
            workflow.group_complete('example', 'group-1', token, accepted=True)
            second = workflow.group_begin('example', 'group-2').active_group.assignment
            self.assertNotEqual(token, second)
            with self.assertRaisesRegex(StateError, 'stale'):
                workflow.begin('example', group_id='group-2', assignment=token)
            self.complete_children('group-2', second, changed=True)
            self.checkpoint('group-2', second)
            state = completion.audit('example')
            self.assertEqual(len(state.records), 4)
            self.assertEqual(len(state.group_records), 2)
            for old, new in zip(state.records, state.records[1:]):
                self.assertEqual(old['end_head'], new['start_head'])
            self.assertIsNone(workflow.group_next('example')['group'])

    def test_pending_checkpoint_partial_report_and_no_change_children(self) -> None:
        import completion
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token)
            facts = json.loads(completion.report('example'))
            self.assertEqual(facts['pending_groups'], ['group-1', 'group-2'])
            self.assertEqual(facts['pending'], ['child-2-1', 'child-2-2'])
            self.assertEqual(facts['records'][0]['outcome'], 'no-change')
            with self.assertRaisesRegex(StateError, 'unresolved'):
                completion.audit('example')
            self.checkpoint('group-1', token)
            self.assertEqual(workflow.group_next('example')['group']['id'], 'group-2')

    def test_checkpoint_write_interruption_before_and_after_markdown(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token, changed=True)
            workflow.group_release('example', 'group-1', token)
            head = workflow.git_output(self.repo, 'rev-parse', 'HEAD')
            with patch('workflow.replace_plan', side_effect=OSError('checkpoint write interrupted')):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            self.assertEqual(workflow.load(self.plan).active_group.phase, 'checkpointing')
            real_save = workflow.save
            def crash(path: Path, state: workflow.Execution) -> None:
                if state.active_group is None:
                    raise OSError('final journal write interrupted')
                real_save(path, state)
            with patch('workflow.save', side_effect=crash):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            self.assertTrue(workflow.read_plan(self.plan, workflow.git_context()).groups[0].completed)
            with self.assertRaisesRegex(StateError, 'transaction|drift'):
                workflow.group_next('example')
            with patch('completion.verification', side_effect=AssertionError('must reuse journaled evidence')):
                state = workflow.group_complete('example', 'group-1', token, accepted=True)
            self.assertEqual(len(state.group_records), 1)
            self.assertEqual(workflow.git_output(self.repo, 'rev-parse', 'HEAD'), head)

    def test_failed_verification_retains_released_assignment_and_evidence(self) -> None:
        with working_directory(self.repo):
            self.plan.write_text(grouped_plan().replace('  - Verify: [', '  - Verify: [', 1))
            token = self.assign()
            self.complete_children('group-1', token)
            workflow.group_release('example', 'group-1', token)
            with patch('completion.verification', return_value={'argv': ['bad'], 'exit_code': 5, 'stderr': 'failure'}):
                with self.assertRaisesRegex(StateError, 'verification failed'):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            state = workflow.load(self.plan)
            self.assertEqual(state.active_group.phase, 'released')
            self.assertEqual(state.active_group.evidence[0]['exit_code'], 5)
            self.assertIn('failed', state.active_group.error)
            self.assertFalse(workflow.read_plan(self.plan, workflow.git_context()).groups[0].completed)
            workflow.group_complete('example', 'group-1', token, accepted=True)

    def test_group_verification_mutation_is_preserved(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token)
            workflow.group_release('example', 'group-1', token)
            def mutate(argv: list[str], path: Path) -> dict[str, object]:
                (path / 'mutation.txt').write_text('retained evidence')
                return {'argv': argv, 'exit_code': 0}
            with patch('completion.verification', side_effect=mutate):
                with self.assertRaisesRegex(StateError, 'mutated'):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            self.assertTrue((self.repo / 'mutation.txt').exists())
            self.assertEqual(workflow.load(self.plan).active_group.phase, 'released')

    def test_release_refuses_active_or_incomplete_children(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            with self.assertRaisesRegex(StateError, 'every assigned child'):
                workflow.group_release('example', 'group-1', token)
            workflow.begin('example', group_id='group-1', assignment=token)
            with self.assertRaisesRegex(StateError, 'child transaction'):
                workflow.group_release('example', 'group-1', token)

    def test_group_revisions_preserve_ids_membership_and_evidence(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token)
            self.checkpoint('group-1', token)
            revision = self.root / 'revision.md'
            revision.write_text(self.plan.read_text().replace('Use plain text files to persist results.',
                                'Use plain text files. Approved decision: keep outputs UTF-8.'))
            workflow.revise('example', revision)
            self.assertIn('UTF-8', self.plan.read_text())
            revision.write_text(self.plan.read_text().replace('Group: group-2', 'Group: renamed'))
            with self.assertRaisesRegex(StateError, 'group IDs'):
                workflow.revise('example', revision)
            revision.write_text(self.plan.read_text().replace('child results work together.', 'changed acceptance', 1))
            with self.assertRaisesRegex(StateError, 'completed group'):
                workflow.revise('example', revision)
            self.assertEqual(len(workflow.load(self.plan).group_records), 1)

    def test_revision_adds_child_to_assigned_group(self) -> None:
        from completion import complete
        def child(n: int, mark: str = ' ') -> str:
            return (f'  - [{mark}] Child 1/{n}\n    - Task: child-1-{n}\n    - Commit: implement child 1 {n}\n'
                    f'    - Paths: ["*.txt"]\n    - Verify: {json.dumps([sys.executable, "-c", "print(123)"])}\n'
                    '    - Done when: child acceptance holds.\n')
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token, changed=True)
            revision = self.root / 'revision.md'
            text = self.plan.read_text()
            for bad, message in [(text.replace('- [ ] Group 2', child(3, 'x') + '- [ ] Group 2'), 'completion'),
                                 (text.replace('  - [x] Child 1/1', child(3) + '  - [x] Child 1/1'), 'completion'),
                                 (text.replace('child-2-1', 'child-1-3'), 'task IDs')]:
                revision.write_text(bad)
                with self.assertRaisesRegex((StateError, PlanFileError), message):
                    workflow.revise('example', revision)
            revision.write_text(text.replace('- [ ] Group 2', child(3) + '- [ ] Group 2'))
            workflow.revise('example', revision)
            with self.assertRaisesRegex(StateError, 'every assigned child'):
                workflow.group_release('example', 'group-1', token)
            self.assertEqual(workflow.begin('example', group_id='group-1', assignment=token).active.task_id, 'child-1-3')
            revision.write_text(self.plan.read_text().replace('  - [ ] Child 1/3', child(4) + '  - [ ] Child 1/3'))
            with self.assertRaisesRegex(StateError, 'follow every completed or active'):
                workflow.revise('example', revision)
            (self.repo / 'group-1-3.txt').write_text('fix-up\n')
            complete('example', group_id='group-1', assignment=token)
            workflow.group_release('example', 'group-1', token)
            with patch('completion.verification', return_value={'argv': [], 'exit_code': 1}):
                with self.assertRaisesRegex(StateError, 'verification failed'):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            revision.write_text(self.plan.read_text().replace('persist results.', 'persist UTF-8 results.'))
            with self.assertRaisesRegex(StateError, 'fix-up child'):
                workflow.revise('example', revision)
            revision.write_text(self.plan.read_text().replace('- [ ] Group 2', child(4) + '- [ ] Group 2'))
            reopened = workflow.revise('example', revision).active_group
            self.assertEqual((reopened.assignment, reopened.phase, reopened.error), (token, 'assigned', None))
            workflow.begin('example', group_id='group-1', assignment=token)
            (self.repo / 'group-1-4.txt').write_text('fix-up\n')
            complete('example', group_id='group-1', assignment=token)
            state = self.checkpoint('group-1', token)
            self.assertEqual(state.group_records[0]['task_ids'], [f'child-1-{n}' for n in range(1, 5)])
            revision.write_text(self.plan.read_text().replace('- [ ] Group 2', child(5) + '- [ ] Group 2'))
            with self.assertRaisesRegex(PlanFileError, 'completion state for group group-1'):
                workflow.revise('example', revision)

    def test_old_flat_journal_defaults_remain_usable(self) -> None:
        import completion
        with working_directory(self.repo):
            workflow.start('example', runner=['unused'])
            state = json.loads(workflow.state_path(self.plan).read_text())
            for key in ('group_protocol', 'active_group', 'group_records'):
                state.pop(key)
            workflow.state_path(self.plan).write_text(json.dumps(state))
            workflow.begin('example')
            completion.complete('example')
            workflow.begin('example')
            completion.complete('example')
            self.assertEqual(completion.audit('example').group_protocol, 0)

    def test_child_checkpoint_recovery_keeps_assignment(self) -> None:
        import completion
        with working_directory(self.repo):
            token = self.assign()
            workflow.begin('example', group_id='group-1', assignment=token)
            (self.repo / 'child.txt').write_text('child')
            with patch('completion.replace_plan', side_effect=OSError('interrupted child')):
                with self.assertRaises(OSError):
                    completion.complete('example', group_id='group-1', assignment=token)
            with self.assertRaisesRegex(StateError, 'stale'):
                completion.complete('example', group_id='group-1', assignment='wrong')
            state = completion.complete('example', group_id='group-1', assignment=token)
            self.assertEqual(state.active_group.assignment, token)
            self.assertEqual(len(state.records), 1)

    def test_audit_detects_group_checkpoint_corruption(self) -> None:
        import completion
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token)
            self.checkpoint('group-1', token)
            second = workflow.group_begin('example', 'group-2').active_group.assignment
            self.complete_children('group-2', second)
            state = self.checkpoint('group-2', second)
            state.group_records[0]['accepted'] = False
            workflow.save(self.plan, state)
            with self.assertRaisesRegex(StateError, 'checkpoint differs'):
                completion.audit('example')
