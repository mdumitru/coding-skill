"""Fresh-process, scoped, repeatable worker handoffs from persisted state."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_workflow as fixtures
from tests.test_workflow import SCRIPTS, working_directory
import workflow
import group_context
from state_io import StateError


class GroupContextTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    assign = fixtures.GroupTransactionTests.assign
    complete_children = fixtures.GroupTransactionTests.complete_children
    checkpoint = fixtures.GroupTransactionTests.checkpoint

    def emit(self, group: str, token: str, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPTS / 'group_context.py'), 'example',
                               '--group', group, '--assignment', token, *options],
                              cwd=self.repo, capture_output=True, text=True)

    def test_scope_decisions_identity_helpers_and_read_only_repeatability(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            before = {p: p.read_bytes() for p in self.plan.parent.rglob('*') if p.is_file()}
            first = self.emit('group-1', token)
            second = self.emit('group-1', token)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            output = json.loads(first.stdout)
            self.assertEqual(output['identity']['path'], str(self.repo))
            self.assertEqual(output['plan_path'], str(self.plan))
            self.assertEqual(output['assignment']['assignment'], token)
            self.assertIn('persist results', output['shared_context'])
            self.assertEqual([t['id'] for t in output['remaining_tasks']], ['child-1-1', 'child-1-2'])
            self.assertNotIn('child-2-1', first.stdout)
            self.assertNotIn('stdout', first.stdout)
            self.assertTrue(Path(output['helpers']['workflow.py']).is_file())
            after = {p: p.read_bytes() for p in self.plan.parent.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_later_worker_reads_approved_decision_and_compact_previous_facts(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            self.complete_children('group-1', token, changed=True)
            self.checkpoint('group-1', token)
            revision = self.root / 'approved.md'
            revision.write_text(self.plan.read_text().replace('Use plain text files to persist results.',
                                'Use plain text files to persist results.\nApproved decision: use UTF-8.'))
            workflow.revise('example', revision)
            second = workflow.group_begin('example', 'group-2').active_group.assignment
            result = self.emit('group-2', second, '--previous-limit', '1')
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertIn('Approved decision: use UTF-8.', output['shared_context'])
            self.assertEqual(output['previous_tasks'][0]['id'], 'child-1-2')
            self.assertEqual(output['previous_groups'][0]['id'], 'group-1')
            self.assertEqual(output['limits']['omitted_tasks'], 1)
            self.assertTrue(output['limits']['truncated'])
            self.assertEqual(output['limits']['details_plan'], str(self.plan))
            self.assertNotIn('print(123)', json.dumps(output['previous_tasks']))
            self.assertNotIn('child-1-1', result.stdout)

    def test_pending_group_after_children_and_clean_begun_child_resume(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            workflow.begin('example', group_id='group-1', assignment=token)
            result = self.emit('group-1', token)
            self.assertEqual(json.loads(result.stdout)['active_task'], 'child-1-1')
            import completion
            completion.complete('example', group_id='group-1', assignment=token)
            workflow.begin('example', group_id='group-1', assignment=token)
            completion.complete('example', group_id='group-1', assignment=token)
            result = self.emit('group-1', token)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['remaining_tasks'], [])
            self.assertEqual(json.loads(result.stdout)['group']['id'], 'group-1')

    def test_wrong_assignment_drift_missing_identity_and_transaction_refused(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            self.assertIn('stale', self.emit('group-1', 'wrong').stderr)
            self.assertIn('stale', self.emit('group-2', token).stderr)
            original = self.plan.read_text()
            self.plan.write_text(original + '\nDrift\n')
            self.assertIn('drift', self.emit('group-1', token).stderr)
            self.plan.write_text(original)
            state = workflow.load(self.plan)
            identity = state.identity
            state.identity = None
            workflow.save(self.plan, state)
            self.assertIn('identity', self.emit('group-1', token).stderr)
            state.identity = identity
            state.active = workflow.ActiveTask('child-1-1', workflow.git_output(self.repo, 'rev-parse', 'HEAD'), phase='committing')
            workflow.save(self.plan, state)
            self.assertIn('journaled child', self.emit('group-1', token).stderr)

    def test_shared_section_required_precise_and_fenced_heading_ignored(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            content = self.plan.read_text()
            revised = content.replace('Use plain text files to persist results.',
                        'Use plain text files to persist results.\n### Interface\nCarry this.\n```markdown\n## Other\nExample context.\n```\n## Investigation\nDo not pass this.')
            revision = self.root / 'revision.md'
            revision.write_text(revised)
            workflow.revise('example', revision)
            context = group_context.context('example', 'group-1', token)
            self.assertIn('Example context.', context.shared_context)
            self.assertIn('Carry this.', context.shared_context)
            self.assertNotIn('Do not pass this.', context.shared_context)
            revision.write_text(self.plan.read_text().replace('## Context and decisions', '## Unscoped context'))
            workflow.revise('example', revision)
            self.assertIn('Context and decisions', self.emit('group-1', token).stderr)

    def test_limit_failure_does_not_silently_truncate_executable_fields(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            limited = self.emit('group-1', token, '--max-chars', '1000')
            self.assertNotEqual(limited.returncode, 0)
            self.assertIn('no tasks or decisions were truncated', limited.stderr)
            self.assertEqual(limited.stdout, '')
            for value in ('-1', '101'):
                self.assertIn('previous-limit', self.emit('group-1', token, '--previous-limit', value).stderr)

    def test_dirty_lock_and_checkpoint_pending_are_refused(self) -> None:
        with working_directory(self.repo):
            token = self.assign()
            (self.repo / 'pending.txt').write_text('retained')
            self.assertIn('pending edits', self.emit('group-1', token).stderr)
            (self.repo / 'pending.txt').unlink()
            from state_io import plan_lock
            with plan_lock(self.plan):
                self.assertIn('locked', self.emit('group-1', token).stderr)
            self.complete_children('group-1', token)
            workflow.group_release('example', 'group-1', token)
            with patch('workflow.replace_plan', side_effect=OSError('checkpoint interrupted')):
                with self.assertRaises(OSError):
                    workflow.group_complete('example', 'group-1', token, accepted=True)
            self.assertIn('unresolved transaction', self.emit('group-1', token).stderr)


if __name__ == '__main__':
    unittest.main()
