#!/usr/bin/env python3
"""Non-mutating mechanical commit-message and staged-path guards."""
from __future__ import annotations

import argparse
import fnmatch
import logging
import re
import subprocess
import sys
from pathlib import Path

LOGGER = logging.getLogger('commit-policy')


def subject_error(message: str, *, allow_body: bool = False) -> str | None:
    """Imperative mood cannot be proved mechanically."""
    lines = message.removesuffix('\n').split('\n')
    if not lines or not re.match(r'^[a-z]', lines[0]) or len(lines[0]) > 80:
        return 'commit subject must start with a lowercase letter and be at most 80 characters'
    for line in lines:
        if re.match(r'^\s*(?:(?:signed-off-by|co-authored-by|reviewed-by|tested-by|acked-by|reported-by):|generated(?:[- ]with|[- ]by)\b)', line, re.I):
            return 'attribution/certification trailers are forbidden'
    if not allow_body and len(lines) != 1:
        return 'commit message must contain one subject and no body/trailers'
    if lines[0] != lines[0].strip():
        return 'commit subject must not have surrounding whitespace'
    return None


def forbidden_path(path: str, extra: list[str] | None = None) -> bool:
    """Known tracking names only; document purpose still needs human judgment."""
    parts = path.split('/')
    return ('_plans' in parts or fnmatch.fnmatchcase(parts[-1].lower(), 'todo*.md')
            or any(fnmatch.fnmatchcase(path, pattern) for pattern in (extra or [])))


def staged_paths(repo: Path) -> list[str]:
    result = subprocess.run(['git', 'diff', '--cached', '--name-only', '--no-renames', '-z'], cwd=repo, capture_output=True, check=True)
    return [p.decode('utf-8', 'surrogateescape') for p in result.stdout.split(b'\0') if p]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    message = sub.add_parser('message')
    source = message.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', type=Path, help='commit message file, e.g. from a hook')
    source.add_argument('--message', help='literal commit message')
    message.add_argument('--allow-body', action='store_true', help='use only when a body was explicitly authorized; trailers remain forbidden')
    paths = sub.add_parser('paths')
    paths.add_argument('--repo', type=Path, default=Path.cwd())
    paths.add_argument('--forbid', action='append', default=[], help='additional forbidden repo-relative path/pattern')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    try:
        if args.operation == 'message':
            message = args.file.read_text(encoding='utf-8') if args.file else args.message
            error = subject_error(message, allow_body=args.allow_body)
            if error:
                raise ValueError(error)
        else:
            forbidden = [p for p in staged_paths(args.repo) if forbidden_path(p, args.forbid)]
            if forbidden:
                raise ValueError(f'forbidden staged tracking paths: {", ".join(forbidden)}')
        print('commit policy passed')
        return 0
    except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
        LOGGER.error('%s', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
