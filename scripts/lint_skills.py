#!/usr/bin/env python3
"""Lint the supported flat skill format and statically local helper imports."""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

LOGGER = logging.getLogger('skill-lint')
REPOSITORY_NAMES = {'AGENTS.md', 'CLAUDE.md'}


@dataclass(frozen=True, order=True)
class Diagnostic:
    path: str
    code: str
    message: str


def visible(content: str) -> str:
    result: list[str] = []
    fence = ''
    for line in content.splitlines():
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            token = match[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ''
        elif not fence:
            result.append(line)
    return '\n'.join(result)


def references(content: str) -> list[str]:
    text = visible(content)
    explicit = re.findall(r'\[[^\]]+\]\(([^)\s]+)\)', text)
    implicit = [s for s in re.findall(r'`([^`\n]+)`', text)
                if re.fullmatch(r'[\w./~-]+\.(?:md|py|sh|json)', s)
                and s not in REPOSITORY_NAMES and not s.startswith('_plans/')]
    explicit = [r[1:-1] if r.startswith('<') and r.endswith('>') else r for r in explicit]
    return [r for r in explicit + implicit if not re.match(r'^[a-z][a-z0-9+.-]*:', r, re.I)
            and not r.startswith('#') and not any(c in r for c in '<>*{}')]


def lint(root: Path) -> list[Diagnostic]:
    root = root.resolve()
    skills = root / 'skills'
    diagnostics: list[Diagnostic] = []
    names: dict[str, str] = {}
    def error(path: Path, code: str, message: str) -> None:
        diagnostics.append(Diagnostic(str(path.relative_to(root)), code, message))
    if not skills.is_dir():
        return [Diagnostic('skills', 'missing-skills', 'skill directory is missing')]
    packages = [p for p in sorted(skills.iterdir()) if p.name != '__pycache__']
    if not packages:
        return [Diagnostic('skills', 'missing-skills', 'no skill packages found')]
    for package in packages:
        if package.is_symlink() or not package.is_dir():
            error(package, 'package-layout', 'skills must be real directories')
            continue
        source = package / 'SKILL.md'
        if not source.is_file() or source.is_symlink():
            error(source, 'missing-skill', 'a regular SKILL.md is required')
            continue
        try:
            content = source.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            error(source, 'read-error', str(exc))
            continue
        lines = content.splitlines()
        if not lines or lines[0] != '---' or '---' not in lines[1:]:
            error(source, 'frontmatter', 'require opening/closing --- delimiters')
        else:
            stop = lines[1:].index('---') + 1
            fields: dict[str, str] = {}
            for line in lines[1:stop]:
                match = re.fullmatch(r'(name|description):\s*(\S.*)', line)
                if not match or match[1] in fields or match[2] in {'|', '>'}:
                    error(source, 'frontmatter', 'only unique single-line name and description scalars are supported')
                    continue
                fields[match[1]] = match[2].strip('"\'')
            if not fields.get('description') or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', fields.get('name', '')):
                error(source, 'frontmatter', 'require a kebab-case name and nonempty description')
            name = fields.get('name', '')
            if name != package.name:
                error(source, 'name-mismatch', 'frontmatter name must match the skill directory')
            if name in names:
                error(source, 'duplicate-name', f'name is also declared by {names[name]}')
            names[name] = str(source.relative_to(root))
        files = [p for p in sorted(package.rglob('*')) if '__pycache__' not in p.parts and p.suffix not in {'.pyc', '.pyo'}]
        for file in files:
            if file.is_symlink():
                error(file, 'symlink', 'source packages must contain real files and directories')
                continue
            if not file.is_file():
                continue
            if file.suffix == '.md':
                try:
                    for reference in references(file.read_text(encoding='utf-8')):
                        target = (file.parent / reference.split('#', 1)[0]).resolve()
                        if not target.is_relative_to(package):
                            error(file, 'reference-escape', f'local reference escapes skill: {reference}')
                        elif not target.exists():
                            error(file, 'missing-reference', f'local reference does not exist: {reference}')
                except (OSError, UnicodeError) as exc:
                    error(file, 'read-error', str(exc))
            if file.suffix == '.py':
                try:
                    code = file.read_text(encoding='utf-8')
                    tree = ast.parse(code, filename=str(file))
                except (OSError, UnicodeError, SyntaxError) as exc:
                    error(file, 'python-source', str(exc))
                    continue
                if 'scripts' in file.relative_to(package).parts:
                    if not code.startswith('#!/usr/bin/env python3\n'):
                        error(file, 'shebang', 'Python scripts require #!/usr/bin/env python3')
                    if not file.stat().st_mode & 0o111:
                        error(file, 'executable', 'Python scripts must be executable')
                for node in ast.walk(tree):
                    imports: list[str] = []
                    if isinstance(node, ast.Import):
                        imports = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:
                            base = file.parent
                            for _ in range(node.level - 1):
                                base = base.parent
                            if not base.is_relative_to(package):
                                error(file, 'import-escape', 'relative import escapes skill')
                            elif node.module and not ((base / node.module.replace('.', '/')).with_suffix('.py').exists() or (base / node.module.replace('.', '/') / '__init__.py').exists()):
                                error(file, 'external-import', f'relative helper is missing: {node.module}')
                            continue
                        if node.module:
                            imports = [node.module]
                    for module in imports:
                        first = module.split('.')[0]
                        if first not in sys.stdlib_module_names and not (file.parent / f'{first}.py').is_file() and not (file.parent / first / '__init__.py').is_file():
                            error(file, 'external-import', f'helper imports outside its package/stdlib: {module}')
    return sorted(set(diagnostics))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        diagnostics = lint(args.root)
    except OSError as exc:
        LOGGER.error('cannot inspect skill packages: %s', exc)
        return 2
    if args.json:
        print(json.dumps([asdict(d) for d in diagnostics], sort_keys=True))
    elif diagnostics:
        for d in diagnostics:
            print(f'{d.path}: {d.code}: {d.message}')
    else:
        print('skill packages passed')
    return int(bool(diagnostics))


if __name__ == '__main__':
    sys.exit(main())
