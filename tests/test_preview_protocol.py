"""Pure and persistence contract tests for the generic preview protocol."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / 'skills/task-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import preview_protocol as pp
from state_io import StateError

LIFECYCLE_SCRIPT = '#!/bin/sh\nexit 0\n'


def protocol_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {'schema_version': 1, 'adapter': 'fixture', 'start_arguments': [{'name': '--seed', 'value': True}, {'name': '--fresh', 'value': False}]}
    data.update(overrides)
    return data


def write_tools(root: Path, protocol: object | None = None, *, skip: str | None = None) -> None:
    tools = root / pp.TOOLS_DIR
    tools.mkdir(parents=True, exist_ok=True)
    (tools / pp.PROTOCOL_FILE).write_text(json.dumps(protocol_data() if protocol is None else protocol))
    for operation in pp.LIFECYCLE:
        if operation == skip:
            continue
        tool = tools / operation
        tool.write_text(LIFECYCLE_SCRIPT)
        tool.chmod(0o755)


def identity(task_id: str = 'task-abc', worktree: str = '/work/example-2', plan_slug: str = 'example') -> pp.RuntimeIdentity:
    return pp.make_identity(plan_slug, task_id, worktree, '/work/.bare')


def envelope(ident: pp.RuntimeIdentity, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {'schema_version': 1, 'operation': 'start', 'success': True, 'plan_slug': ident.plan_slug,
                               'task_id': ident.task_id, 'worktree': ident.worktree, 'runtime_id': ident.runtime_id,
                               'status': 'running', 'readiness': {'ready': True, 'detail': 'healthy'},
                               'services': [{'name': 'web', 'url': f'http://{ident.hostname}:8080/', 'port': 8080}],
                               'details': {'database_password': 'hunter2', 'nested': {'api_key': 'k', 'size': 3}}}
    data.update(overrides)
    return data


def claim(ident: pp.RuntimeIdentity, pairing: str | None = None) -> pp.Claim:
    return pp.Claim(ident.hostname, ident.runtime_id, ident.task_id, ident.worktree, ident.common_dir, f'{ident.worktree}/runtime.json', pairing)


class PureDecisionTests(unittest.TestCase):
    def test_hostname_is_exact_plan_slug(self) -> None:
        self.assertEqual(pp.hostname_for('example'), 'example.localhost')
        ident = identity(worktree='/work/example-2')
        self.assertEqual(ident.hostname, 'example.localhost')
        for bad in ('Example', 'example.md', '../x', 'a_b', ''):
            with self.subTest(bad=bad), self.assertRaises(StateError):
                pp.hostname_for(bad)

    def test_runtime_id_is_deterministic_and_identity_scoped(self) -> None:
        self.assertEqual(identity().runtime_id, identity().runtime_id)
        self.assertNotEqual(identity().runtime_id, identity(task_id='other').runtime_id)
        with self.assertRaises(StateError):
            pp.make_identity('example', 'bad id', '/w', '/c')
        with self.assertRaises(StateError):
            pp.make_identity('example', 'id', 'relative', '/c')

    def test_protocol_validation(self) -> None:
        protocol = pp.parse_protocol(protocol_data(pairing={'group': 'shop'}))
        self.assertEqual(protocol.adapter, 'fixture')
        self.assertEqual(protocol.pairing, 'shop')
        self.assertEqual([o.name for o in protocol.start_options], ['--seed', '--fresh'])
        malformed = [
            ([], 'JSON object'),
            (protocol_data(schema_version=2), 'incompatible'),
            (protocol_data(schema_version=True), 'integer'),
            (protocol_data(schema_version='1'), 'integer'),
            (protocol_data(adapter='Bad Kind'), 'adapter'),
            (protocol_data(extra=1), 'unknown'),
            (protocol_data(start_arguments={'--seed': True}), 'list'),
            (protocol_data(start_arguments=[{'name': 'seed'}]), 'malformed'),
            (protocol_data(start_arguments=[{'name': '--seed'}, {'name': '--seed'}]), 'duplicate'),
            (protocol_data(start_arguments=[{'name': '--state-dir'}]), 'reserved'),
            (protocol_data(start_arguments=[{'name': '--seed', 'shell': 'x'}]), 'only name'),
            (protocol_data(pairing='shop'), 'pairing'),
        ]
        for data, message in malformed:
            with self.subTest(data=data), self.assertRaisesRegex(StateError, message):
                pp.parse_protocol(data)

    def test_start_arguments_are_validated_against_declarations(self) -> None:
        protocol = pp.parse_protocol(protocol_data())
        self.assertEqual(pp.validate_start_arguments(protocol, ['--seed', 'a b; rm -rf /', '--fresh', '--seed=x']),
                         ['--seed', 'a b; rm -rf /', '--fresh', '--seed=x'])
        for argv, message in ((['--other'], 'unsupported'), (['positional'], 'unsupported'), (['--seed'], 'requires a value'),
                              (['--fresh=1'], 'does not take'), (['--fresh\x00'], 'NUL')):
            with self.subTest(argv=argv), self.assertRaisesRegex(StateError, message):
                pp.validate_start_arguments(protocol, argv)

    def test_lifecycle_argv_carries_identity_flags(self) -> None:
        ident = identity()
        argv = pp.lifecycle_argv(Path('/r/worktree-tools/start'), ident, Path('/s'), '--', '--seed', 'x')
        self.assertEqual(argv[:10], ['/r/worktree-tools/start', '--plan-slug', 'example', '--hostname', 'example.localhost',
                                     '--task-id', 'task-abc', '--state-dir', '/s', '--json'])
        self.assertEqual(argv[10:], ['--', '--seed', 'x'])
        self.assertNotIn('--json', pp.lifecycle_argv(Path('/t'), ident, Path('/s'), machine=False))

    def test_response_validation_redacts_and_bounds(self) -> None:
        ident = identity()
        response = pp.parse_response(envelope(ident), ident, 'start')
        self.assertEqual(response.services[0].port, 8080)
        self.assertEqual(response.details['database_password'], '<redacted>')
        self.assertEqual(response.details['nested'], {'api_key': '<redacted>', 'size': 3})
        self.assertIsNone(pp.readiness_error(pp.parse_protocol(protocol_data()), response))
        long = pp.parse_response(envelope(ident, readiness={'ready': True, 'detail': 'token=abc ' + 'x' * 5000}), ident, 'start')
        self.assertNotIn('abc', long.readiness_detail)
        self.assertLess(len(long.readiness_detail), 1100)
        self.assertIn('<redacted>', pp.bounded('connect https://user:pw@db/x password: "p w"'))
        self.assertNotIn('pw@', pp.bounded('connect https://user:pw@db/x'))

    def test_response_rejects_foreign_identity_and_bad_shape(self) -> None:
        ident = identity()
        foreign = identity(task_id='other')
        cases = [
            (envelope(ident, schema_version=2), 'incompatible'),
            (envelope(ident, operation='stop'), 'operation'),
            (envelope(ident, task_id='other'), 'foreign'),
            (envelope(ident, worktree='/elsewhere'), 'foreign'),
            (envelope(ident, runtime_id=foreign.runtime_id), 'foreign'),
            (envelope(ident, status='weird'), 'status'),
            (envelope(ident, readiness=True), 'readiness'),
            (envelope(ident, services=[{'name': 'web', 'url': 'http://other.localhost:1/', 'port': 1}]), 'exactly'),
            (envelope(ident, services=[{'name': 'web', 'url': 'http://example.localhost.evil:1/', 'port': 1}]), 'exactly'),
            (envelope(ident, services=[{'name': 'web', 'url': 'http://u:p@example.localhost:1/', 'port': 1}]), 'credentials'),
            (envelope(ident, services=[{'name': 'web', 'url': 'http://example.localhost:2/', 'port': 1}]), 'disagrees'),
            (envelope(ident, services=[{'name': 'web', 'url': 'http://example.localhost/', 'port': True}]), 'port'),
            (envelope(ident, services=[{'name': 'a', 'url': 'http://example.localhost/', 'port': 1}, {'name': 'a', 'url': 'http://example.localhost/', 'port': 2}]), 'unique'),
            (envelope(ident, secret='x'), 'unknown'),
            (envelope(ident, details=[]), 'details'),
        ]
        for data, message in cases:
            with self.subTest(message=message, data=data), self.assertRaisesRegex(StateError, message):
                pp.parse_response(data, ident, 'start')
        with self.assertRaisesRegex(StateError, 'exceeds'):
            pp.parse_json_text(json.dumps({'x': 'y' * pp.MAX_JSON_BYTES}), 'adapter response')

    def test_readiness_requires_running_ready_services_and_pairing(self) -> None:
        ident = identity()
        protocol = pp.parse_protocol(protocol_data())
        paired = pp.parse_protocol(protocol_data(pairing={'group': 'shop'}))
        for data, proto, message in ((envelope(ident, success=False), protocol, 'failure'),
                                     (envelope(ident, status='starting'), protocol, 'not ready'),
                                     (envelope(ident, readiness={'ready': False}), protocol, 'not ready'),
                                     (envelope(ident, services=[]), protocol, 'no services'),
                                     (envelope(ident), paired, 'pairing'),
                                     (envelope(ident, pairing={'group': 'shop'}), protocol, 'pairing')):
            with self.subTest(message=message):
                self.assertIn(message, pp.readiness_error(proto, pp.parse_response(data, ident, 'start')) or '')
        self.assertIsNone(pp.readiness_error(paired, pp.parse_response(envelope(ident, pairing={'group': 'shop'}), ident, 'start')))

    def test_independent_hostname_claims_collide_but_pairs_coexist(self) -> None:
        first = identity(worktree='/work/a', task_id='a')
        second = pp.make_identity('example', 'b', '/other/b', '/other/.bare')
        claims, error = pp.claim_decision([], claim(first))
        self.assertIsNone(error)
        self.assertEqual(pp.claim_decision(claims, claim(first))[0], claims)
        _, error = pp.claim_decision(claims, claim(second))
        self.assertIn('independent runtime', error or '')
        paired, error = pp.claim_decision([claim(first, 'shop')], claim(second, 'shop'))
        self.assertIsNone(error)
        self.assertEqual(len(paired), 2)
        _, error = pp.claim_decision([claim(first, 'shop')], claim(second, 'other'))
        self.assertIn('independent', error or '')
        _, error = pp.claim_decision([claim(first, 'shop')], claim(second))
        self.assertIn('independent', error or '')
        hijack = replace(claim(first), worktree='/elsewhere')
        _, error = pp.claim_decision(claims, hijack)
        self.assertIn('foreign', error or '')
        remaining, released, error = pp.release_decision(paired, first.hostname, first.runtime_id, 'wrong')
        self.assertIn('foreign', error or '')
        remaining, released, error = pp.release_decision(paired, first.hostname, first.runtime_id, first.task_id)
        self.assertTrue(released)
        self.assertEqual([c.runtime_id for c in remaining], [second.runtime_id])

    def test_record_identity_and_corruption(self) -> None:
        ident = identity()
        record = pp.RuntimeRecord(1, ident.runtime_id, ident.plan_slug, ident.hostname, ident.task_id, ident.worktree, ident.common_dir, 'fixture', '/s', 'running')
        from dataclasses import asdict
        data = asdict(record)
        self.assertEqual(pp.check_record(data, ident), record)
        with self.assertRaisesRegex(StateError, 'foreign'):
            pp.check_record(data, identity(task_id='other'))
        with self.assertRaisesRegex(StateError, 'foreign'):
            pp.check_record({**data, 'worktree': '/x'}, ident)
        for bad, message in (({**data, 'schema_version': 2}, 'incompatible'), ({**data, 'phase': 'dancing'}, 'phase'),
                             ({**data, 'surprise': 1}, 'malformed'), ({**data, 'start_args': [1]}, 'start_args'), ([], 'object')):
            with self.subTest(message=message), self.assertRaisesRegex(StateError, message):
                pp.check_record(bad, ident)

    def test_is_within_is_pure(self) -> None:
        self.assertTrue(pp.is_within(Path('/a'), Path('/a/b')))
        self.assertTrue(pp.is_within(Path('/a'), Path('/a')))
        self.assertFalse(pp.is_within(Path('/a'), Path('/ab')))


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.task = self.root / 'workspace/example-2'
        self.task.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_protocol_requires_every_executable(self) -> None:
        with self.assertRaisesRegex(StateError, 'does not support previews'):
            pp.load_protocol(self.task)
        write_tools(self.task, skip='reset')
        with self.assertRaisesRegex(StateError, 'worktree-tools/reset'):
            pp.load_protocol(self.task)
        write_tools(self.task)
        (self.task / pp.TOOLS_DIR / 'logs').chmod(0o644)
        with self.assertRaisesRegex(StateError, 'worktree-tools/logs'):
            pp.load_protocol(self.task)
        (self.task / pp.TOOLS_DIR / 'logs').chmod(0o755)
        self.assertEqual(pp.load_protocol(self.task).adapter, 'fixture')
        write_tools(self.task, protocol_data(schema_version=9))
        with self.assertRaisesRegex(StateError, 'incompatible'):
            pp.load_protocol(self.task)

    def test_symlink_escapes_are_refused(self) -> None:
        outside = self.root / 'outside'
        write_tools(outside)
        (self.task / pp.TOOLS_DIR).symlink_to(outside / pp.TOOLS_DIR)
        with self.assertRaisesRegex(StateError, 'symlink'):
            pp.load_protocol(self.task)
        (self.task / pp.TOOLS_DIR).unlink()
        write_tools(self.task)
        (self.task / pp.TOOLS_DIR / 'stop').unlink()
        (self.task / pp.TOOLS_DIR / 'stop').symlink_to(outside / pp.TOOLS_DIR / 'stop')
        with self.assertRaisesRegex(StateError, 'symlink'):
            pp.adapter_executable(self.task, 'stop')
        workspace = self.root / 'workspace'
        (workspace / pp.FAUR_RUNS).symlink_to(outside)
        with self.assertRaisesRegex(StateError, 'symlink'):
            pp.runtime_paths(workspace, 'example', faur=True)
        with self.assertRaises(StateError):
            pp.safe_child(workspace, '..', 'outside')

    def test_records_are_atomic_private_and_identity_checked(self) -> None:
        workspace = self.root / 'workspace'
        ident = pp.make_identity('example', 'task-1', str(self.task), str(workspace / '.bare'))
        paths = pp.runtime_paths(workspace, 'example', faur=True)
        self.assertEqual(paths.directory, workspace / '_runs/example')
        self.assertEqual(pp.runtime_paths(workspace, 'example', faur=False).root.name, '.worktree-runs')
        with pp.runtime_lock(paths, ident):
            self.assertIsNone(pp.load_record(paths, ident))
            with self.assertRaisesRegex(StateError, 'locked'):
                with pp.runtime_lock(paths, ident):
                    pass
            record = pp.RuntimeRecord(1, ident.runtime_id, ident.plan_slug, ident.hostname, ident.task_id, ident.worktree,
                                      ident.common_dir, 'fixture', str(paths.state_dir), 'starting')
            pp.save_record(paths, record)
        self.assertFalse(paths.lock.exists())
        self.assertEqual(stat.S_IMODE(paths.record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.state_dir.stat().st_mode), 0o700)
        self.assertEqual(pp.load_record(paths, ident).phase, 'starting')
        foreign = pp.make_identity('example', 'task-2', str(self.task), str(workspace / '.bare'))
        with self.assertRaisesRegex(StateError, 'foreign'):
            pp.load_record(paths, foreign)
        paths.record.write_text('{corrupt')
        with self.assertRaisesRegex(StateError, 'not valid JSON'):
            pp.load_record(paths, ident)
        self.assertEqual(paths.record.read_text(), '{corrupt')

    def test_stale_lock_is_evidence_not_cleared(self) -> None:
        workspace = self.root / 'workspace'
        ident = pp.make_identity('example', 'task-1', str(self.task), str(workspace / '.bare'))
        paths = pp.runtime_paths(workspace, 'example', faur=True)
        pp.ensure_runtime_dirs(paths)
        paths.lock.write_text('{"pid": 999999}')
        with self.assertRaisesRegex(StateError, 'inspect its PID'):
            with pp.runtime_lock(paths, ident):
                pass
        self.assertTrue(paths.lock.exists())

    def test_global_registry_rejects_independent_duplicates(self) -> None:
        registry = pp.registry_dir({'XDG_STATE_HOME': str(self.root / 'state'), 'HOME': str(self.root)})
        self.assertEqual(registry, self.root / 'state/coding-skill/preview-hostnames')
        self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o700)
        self.assertEqual(pp.registry_dir({'HOME': str(self.root)}), self.root / '.local/state/coding-skill/preview-hostnames')
        first = pp.make_identity('example', 'a', '/repo-a/example', '/repo-a/.bare')
        second = pp.make_identity('example', 'b', '/repo-b/example', '/repo-b/.bare')
        pp.claim_hostname(registry, claim(first))
        pp.claim_hostname(registry, claim(first))
        with self.assertRaisesRegex(StateError, 'independent runtime'):
            pp.claim_hostname(registry, claim(second))
        self.assertEqual(len(pp.read_claims(registry, 'example.localhost')), 1)
        self.assertTrue(pp.release_hostname(registry, first.hostname, first.runtime_id, first.task_id))
        self.assertFalse((registry / 'example.localhost.json').exists())
        self.assertFalse(pp.release_hostname(registry, first.hostname, first.runtime_id, first.task_id))
        pp.claim_hostname(registry, claim(first, 'shop'))
        pp.claim_hostname(registry, claim(second, 'shop'))
        self.assertEqual(len(pp.read_claims(registry, 'example.localhost')), 2)
        (registry / 'example.localhost.json').write_text('[]')
        with self.assertRaisesRegex(StateError, 'corrupt'):
            pp.claim_hostname(registry, claim(first, 'shop'))
        self.assertEqual((registry / 'example.localhost.json').read_text(), '[]')


class CompatibilityTests(unittest.TestCase):
    def test_plan_lock_message_and_state_paths_are_unchanged(self) -> None:
        from state_io import plan_lock
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary).resolve() / '_plans/example.md'
            plan.parent.mkdir()
            plan.write_text('# TODO: x\n')
            with plan_lock(plan):
                self.assertTrue((plan.parent / '.state/example.lock').exists())
                with self.assertRaisesRegex(StateError, 'plan is locked'):
                    with plan_lock(plan):
                        pass
            self.assertFalse((plan.parent / '.state/example.lock').exists())
            self.assertNotIn('.state', {pp.FAUR_RUNS, pp.PLAIN_RUNS})


if __name__ == '__main__':
    unittest.main()
