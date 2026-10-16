"""AST 静态守卫：捕获"模块内被使用但未导入/未定义"的模块级名字。

防 B01 类回归（跨模块裸函数丢 import 导致的运行时 NameError）。
处理：闭包链、for/with/except/walrus 目标、推导式变量、try 内 import、dunder。
用法: python scripts/check_imports.py [要检查的根目录...]  默认 src/suwan
退出码：0=干净，1=发现问题。
"""
from __future__ import annotations

import ast
import builtins
import pathlib
import sys

BUILTINS = frozenset(dir(builtins))
_DUNDERS = frozenset(
    {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__", "__cached__", "__builtins__"}
)
_SKIP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def iter_py_files(roots: list[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for raw in roots:
        p = pathlib.Path(raw)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if "__pycache__" not in f.parts:
                    files.append(f)
        else:
            print(f"[check_imports] 跳过不存在的路径: {raw}")
    return files


def _store_of(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
    return names


def _iter_scope(node: ast.AST):
    """遍历 node 所在作用域的语句节点；不进入嵌套函数/类/lambda 体。"""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, _SKIP) and cur is not node:
            continue
        yield cur
        stack.extend(ast.iter_child_nodes(cur))


def _iter_scope_visible(node: ast.AST):
    """同 _iter_scope，但会"看到"嵌套 def/class 节点本身（不深入其体）。"""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, _SKIP) and cur is not node:
            continue
        stack.extend(ast.iter_child_nodes(cur))


def _module_globals(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    # 顶层 def/class 名
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    # 顶层作用域内（不深入函数/类）的 import 与赋值
    for node in _iter_scope(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            target = getattr(node, "target", None) or getattr(node, "targets", None)
            if isinstance(target, ast.AST):
                names |= _store_of(target)
            elif isinstance(target, list):
                for t in target:
                    names |= _store_of(t)
    return names


def _own_locals(func: ast.FunctionDef) -> set[str]:
    """函数自身作用域内可引用的名字（不含闭包外层）。"""
    local: set[str] = set()
    args = func.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        local.add(arg.arg)
    if args.vararg:
        local.add(args.vararg.arg)
    if args.kwarg:
        local.add(args.kwarg.arg)

    for node in _iter_scope_visible(func):
        if node is func:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(node.name)  # 本作用域直接定义的函数/类（可被递归引用）
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            target = getattr(node, "target", None) or getattr(node, "targets", None)
            if isinstance(target, ast.AST):
                local |= _store_of(target)
            elif isinstance(target, list):
                for t in target:
                    local |= _store_of(t)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            local |= _store_of(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    local |= _store_of(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                local.add(node.name)
        elif isinstance(node, ast.NamedExpr):
            local |= _store_of(node.target)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                local |= _store_of(gen.target)
    return local


def _check_usage(func: ast.FunctionDef, effective_locals: set[str], module_globals: set[str], issues: list) -> None:
    for node in _iter_scope(func):
        if node is func:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if name in BUILTINS or name in _DUNDERS or name in module_globals or name in effective_locals:
                continue
            if name in {"self", "cls", "super"}:
                continue
            issues.append((getattr(node, "lineno", 0), f"函数 {func.name}() 内使用未定义/未导入名字 '{name}'"))


def _walk_functions(node: ast.AST, outer_locals: set[str], module_globals: set[str], issues: list) -> None:
    """DFS 遍历（含嵌套函数），维护闭包链上的局部名。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            own = _own_locals(child)
            effective = own | outer_locals
            _check_usage(child, effective, module_globals, issues)
            _walk_functions(child, effective, module_globals, issues)
        elif isinstance(child, ast.ClassDef):
            _walk_functions(child, outer_locals, module_globals, issues)  # 类作用域不参与闭包链


def _check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    issues: list[tuple[int, str]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [(0, f"解析失败: {exc}")]

    module_globals = _module_globals(tree)

    # 模块级语句中的使用（不在函数/类体内）
    for node in _iter_scope(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in BUILTINS and node.id not in _DUNDERS and node.id not in module_globals:
                issues.append((getattr(node, "lineno", 0), f"模块级使用未定义名字 '{node.id}'"))

    # 函数体内
    _walk_functions(tree, set(), module_globals, issues)
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    roots = argv or [str(pathlib.Path(__file__).resolve().parents[1] / "src")]
    total = 0
    for path in iter_py_files(roots):
        issues = _check_file(path)
        if issues:
            total += len(issues)
            print(f"\n{path}")
            for lineno, msg in issues:
                print(f"  L{lineno}: {msg}")
    if total:
        print(f"\n[check_imports] 发现 {total} 个问题")
        return 1
    print("[check_imports] OK：未发现使用未导入/未定义模块级名字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
