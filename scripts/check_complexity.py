#!/usr/bin/env python3
"""Enforce the repository's per-function McCabe complexity ratchet."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable, TypeGuard

import mccabe


DEFAULT_SOURCES = ("hermes_wisdom_plugin", "dashboard/plugin_api.py", "__init__.py")


class CheckError(Exception):
    """A deterministic checker input or analysis failure."""


class _IdentityCheckingVisitor(mccabe.PathGraphingAstVisitor):
    def __init__(self, relative_path: str) -> None:
        super().__init__()
        self.relative_path = relative_path

    def _reject_duplicate(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.graph is None:
            entity = f"{self.classname}{node.name}" if self.classname else node.name
            if entity in self.graphs:
                raise CheckError(
                    f"duplicate function identity: {self.relative_path}::{entity}"
                )

    def visitFunctionDef(self, node: ast.FunctionDef) -> None:
        self._reject_duplicate(node)
        super().visitFunctionDef(node)

    def visitAsyncFunctionDef(  # pylint: disable=invalid-name
        self, node: ast.AsyncFunctionDef
    ) -> None:
        """Visit an async function using McCabe's function implementation."""
        self._reject_duplicate(node)
        super().visitFunctionDef(node)


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise CheckError(f"path is outside repository root: {path}") from exc


def _staged_paths(root: Path, sources: Iterable[str]) -> list[str]:
    output = _git(root, "ls-files", "-z", "--cached", "--", *sources)
    paths = output.decode("utf-8", errors="strict").split("\0")
    return sorted(path for path in paths if path.endswith(".py"))


def _working_paths(root: Path, sources: Iterable[str]) -> list[str]:
    paths: set[str] = set()
    for source in sources:
        candidate = root / source
        if candidate.is_file() and candidate.suffix == ".py":
            paths.add(_relative(root, candidate))
        elif candidate.is_dir():
            paths.update(_relative(root, path) for path in candidate.rglob("*.py"))
    return sorted(paths)


def _read_text(root: Path, relative_path: str, staged: bool) -> str:
    try:
        data = (
            _git(root, "show", f":{relative_path}")
            if staged
            else (root / relative_path).read_bytes()
        )
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckError(f"{relative_path}: source is not valid UTF-8") from exc
    except OSError as exc:
        raise CheckError(f"{relative_path}: cannot read source: {exc}") from exc


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_baseline(root: Path, path: Path, staged: bool) -> dict[str, object]:
    relative = _relative(root, path)
    try:
        raw = _read_text(root, relative, staged)
        baseline = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckError(f"{relative}: invalid baseline JSON: {exc}") from exc
    if not isinstance(baseline, dict) or baseline.get("schema") != 1:
        raise CheckError(f"{relative}: unsupported complexity baseline schema")
    engine = baseline.get("engine")
    expected = {"name": "mccabe", "version": mccabe.__version__}
    if engine != expected:
        raise CheckError(f"{relative}: expected engine {expected}, found {engine}")
    default_max = baseline.get("default_max")
    if not _is_plain_int(default_max) or default_max <= 0:
        raise CheckError(f"{relative}: default_max must be a positive integer")
    functions = baseline.get("functions")
    if not isinstance(functions, dict) or any(
        not isinstance(key, str)
        or not _is_plain_int(value)
        or value <= default_max
        for key, value in functions.items()
    ):
        raise CheckError(
            f"{relative}: functions complexities must be integers above default_max"
        )
    return baseline


def _complexities(relative_path: str, source: str) -> dict[str, int]:
    try:
        tree = compile(source, relative_path, "exec", ast.PyCF_ONLY_AST)
    except SyntaxError as exc:
        raise CheckError(
            f"{relative_path}:{exc.lineno}:{exc.offset}: syntax error: {exc.msg}"
        ) from exc
    visitor = _IdentityCheckingVisitor(relative_path)
    visitor.preorder(tree, visitor)
    return {
        f"{relative_path}::{entity}": graph.complexity()
        for entity, graph in visitor.graphs.items()
    }


def _measure(args: argparse.Namespace, root: Path) -> dict[str, int]:
    paths = (
        _staged_paths(root, args.source)
        if args.staged
        else _working_paths(root, args.source)
    )
    current: dict[str, int] = {}
    for path in paths:
        measured = _complexities(path, _read_text(root, path, args.staged))
        overlap = current.keys() & measured.keys()
        if overlap:
            raise CheckError(f"duplicate function identity: {sorted(overlap)[0]}")
        current.update(measured)
    return current


def check(args: argparse.Namespace) -> int:
    """Measure sources and either print a baseline or enforce the ratchet."""
    root = args.root.resolve()
    current = _measure(args, root)
    if args.print_baseline:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "engine": {"name": "mccabe", "version": mccabe.__version__},
                    "default_max": 10,
                    "functions": {
                        identity: complexity
                        for identity, complexity in sorted(current.items())
                        if complexity > 10
                    },
                },
                indent=2,
            )
        )
        return 0
    baseline = _load_baseline(root, args.baseline.resolve(), args.staged)

    default_max = baseline["default_max"]
    recorded = baseline["functions"]
    assert isinstance(default_max, int)
    assert isinstance(recorded, dict)
    failures: list[str] = []
    for identity, complexity in sorted(current.items()):
        prior = recorded.get(identity)
        allowed = default_max if prior is None else prior
        if complexity > allowed:
            suffix = " (new function)" if prior is None else ""
            failures.append(
                f"{identity} complexity {complexity} exceeds allowed {allowed}{suffix}"
            )

    removals = sorted(recorded.keys() - current.keys())
    resolved = sorted(
        identity
        for identity in current.keys() & recorded.keys()
        if current[identity] <= default_max
    )
    for identity in removals:
        print(f"removed baseline exception: {identity}")
    for identity in resolved:
        print(
            f"resolved baseline exception: {identity} "
            f"(complexity {current[identity]} <= {default_max})"
        )
    if failures:
        for failure in failures:
            print(f"complexity error: {failure}", file=sys.stderr)
        return 1
    print(
        f"complexity ratchet passed: {len(current)} functions checked "
        f"({len(recorded)} baseline exceptions, {len(removals)} removed)"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("scripts/complexity-baseline.json"),
    )
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument(
        "--print-baseline",
        action="store_true",
        help="print a complete baseline to stdout; never writes or accepts it",
    )
    return parser


def main() -> int:
    """Parse command-line arguments and return a process exit status."""
    parser = _parser()
    args = parser.parse_args()
    if not args.source:
        args.source = list(DEFAULT_SOURCES)
    try:
        return check(args)
    except CheckError as exc:
        print(f"complexity error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
