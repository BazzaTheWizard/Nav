"""Round 66: what the gaming app still reaches, by following imports. Nothing is deleted or
changed: this only reads the code. docs/PRUNE_CANDIDATES.md is written from its output.

  python tools/import_graph.py            a summary
  python tools/import_graph.py --json     everything, for the doc or another script

How it reads the code (ast, no imports are executed):
- Every `import` / `from ... import` in every project file is an edge, including the ones
  inside functions (Nav imports a lot lazily). Following every edge from the entry points
  gives the modules the gaming app can load.
- app.py is one 4,000-line file that holds both apps, so it is read function by function:
  starting from main() and the module's own top-level code, without `_main_b2b` (the branch
  NAV_MODE=b2b takes), which functions can run, and which imported names do they use? An import
  line at the top of app.py that only B2B functions use does not count as reaching its module.
- Entry points: app.py in gaming mode, gta_tab.py, tools/narrate.py, tools/clip_stream.py,
  tools/find_leads.py, core/tool_jobs.py (it runs itself as the job wrapper).

- A second, finer pass (call_reach) follows names instead of import lines, in every file: a
  function counts as reached when reached code mentions it (calls it, passes it, subclasses
  it), a module as loaded when a loaded module imports it at the top or a reached function
  imports it inside. That finds modules that are imported but never called. It is a reading of
  names, not a proof: see "What it cannot see".

What it cannot see: a module loaded by name at run time (importlib, a subprocess running a
script), a function reached only through getattr or a string, a method called on an object
(so a class is counted whole as soon as its name is mentioned). Read the doc's caveats before
deleting anything.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_MODULES = ("gta_tab", "tools.narrate", "tools.clip_stream", "tools.find_leads", "core.tool_jobs")
APP = "app"
APP_ROOT_FUNCTIONS = ("main",)
APP_B2B_ONLY = ("_main_b2b",)  # the branch main() takes with NAV_MODE=b2b


def project_modules(root: Path = ROOT) -> dict[str, Path]:
    """{"core.clip_mode.chat": path, ...} for the app's own code (not tests, not .venv)."""
    files = [p for p in root.glob("*.py")] + list((root / "core").rglob("*.py")) + list((root / "tools").glob("*.py"))
    out: dict[str, Path] = {}
    for path in files:
        if "__pycache__" in path.parts or path.resolve() == Path(__file__).resolve():
            continue  # (this file is a developer's tool, not part of the app)
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        out[".".join(parts)] = path
    return out


def _targets(node: ast.AST, module: str, is_package: bool, known: dict[str, Path]) -> list[tuple[str, str]]:
    """The project modules an import statement loads, as (local name it binds, module)."""
    found: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            for i in range(len(parts), 0, -1):
                name = ".".join(parts[:i])
                if name in known:
                    found.append((alias.asname or parts[0], name))
                    break
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            package = module.split(".") if is_package else module.split(".")[:-1]
            package = package[:len(package) - (node.level - 1)]
            base = ".".join([*package, base] if base else package)
        for alias in node.names:
            deeper = f"{base}.{alias.name}" if base else alias.name
            target = deeper if deeper in known else base if base in known else None
            if target:
                found.append((alias.asname or alias.name, target))
    return found


def _bindings(node: ast.AST, module: str, is_package: bool, known: dict[str, Path]) -> list[tuple[str, str, str | None]]:
    """What an import statement binds: (local name, project module, attribute). The attribute is
    None when the local name is the module itself (`from core import series`, `import x as y`),
    "*" for a star import, else the name taken from the module (`from core.fit import buyer_fit`)."""
    out: list[tuple[str, str, str | None]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            for i in range(len(parts), 0, -1):
                name = ".".join(parts[:i])
                if name in known:
                    # `import core.x.y` binds "core": uses of it cannot be followed, so "*" (count it whole)
                    out.append((alias.asname or parts[0], name, None if alias.asname or len(parts) == 1 else "*"))
                    break
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            package = module.split(".") if is_package else module.split(".")[:-1]
            package = package[:len(package) - (node.level - 1)]
            base = ".".join([*package, base] if base else package)
        for alias in node.names:
            deeper = f"{base}.{alias.name}" if base else alias.name
            if deeper in known:
                out.append((alias.asname or alias.name, deeper, None))
            elif base in known:
                out.append((alias.asname or alias.name, base, alias.name))
    return out


def _is_main_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__")


class _Module:
    """One file, read once: its top-level functions and classes, what its imports bind, and
    which names each piece of it mentions."""

    def __init__(self, name: str, path: Path, known: dict[str, Path], entry: bool) -> None:
        self.name = name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = path.name == "__init__.py"
        self.defs: dict[str, ast.AST] = {}
        self.bound: dict[str, tuple[str, str | None]] = {}
        self.top_imports: set[str] = set()
        self.star: set[str] = set()
        top: list[ast.AST] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = node
                top += node.decorator_list  # decorators and default values run when the module loads
                if not isinstance(node, ast.ClassDef):
                    top += [d for d in (*node.args.defaults, *node.args.kw_defaults) if d is not None]
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._bind(node, name, package, known, self.bound, self.top_imports)
            elif _is_main_guard(node) and not entry:
                continue  # `if __name__ == "__main__"` only runs in a file started as a script
            else:
                top.append(node)
                for inner in ast.walk(node):  # an import inside try: / if: at the top still runs at load
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        self._bind(inner, name, package, known, self.bound, self.top_imports)
        self.top_uses = self._uses(top)
        self.uses: dict[str, tuple[set[str], set[tuple[str, str]], dict[str, tuple[str, str | None]], set[str]]] = {}
        for def_name, node in self.defs.items():
            local: dict[str, tuple[str, str | None]] = {}
            lazy: set[str] = set()
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    self._bind(inner, name, package, known, local, lazy)
            names, attrs = self._uses([node])
            self.uses[def_name] = (names, attrs, local, lazy)

    def _bind(self, node, name, package, known, into: dict, modules: set) -> None:
        for local, target, attr in _bindings(node, name, package, known):
            if attr == "*":
                self.star.add(target)
            into[local] = (target, None if attr == "*" else attr)
            modules.add(target)

    @staticmethod
    def _uses(nodes: list[ast.AST]) -> tuple[set[str], set[tuple[str, str]]]:
        """(names mentioned on their own, (name, attribute) pairs for `name.attribute`)."""
        bare: set[str] = set()
        attrs: set[tuple[str, str]] = set()
        for top in nodes:
            based: set[int] = set()
            for node in ast.walk(top):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    attrs.add((node.value.id, node.attr))
                    based.add(id(node.value))
            for node in ast.walk(top):
                if isinstance(node, ast.Name) and id(node) not in based:
                    bare.add(node.id)
        return bare, attrs


def call_reach(known: dict[str, Path], entries: tuple[str, ...] = (APP, *ENTRY_MODULES),
               without: dict[str, tuple[str, ...]] | None = None) -> dict:
    """Follow names from the entry points. Returns {"loaded": modules that get imported,
    "reached": {module: [its functions and classes that reached code mentions]},
    "defs": {module: all of them}}. ``without``: {module: functions never entered}."""
    without = without if without is not None else {APP: APP_B2B_ONLY}
    mods = {name: _Module(name, path, known, name in entries) for name, path in known.items()}
    loaded: set[str] = set()
    reached: dict[str, set[str]] = {name: set() for name in known}
    why: dict[str, dict[str, str]] = {name: {} for name in known}  # module -> def -> "module.def" that mentioned it first
    todo: list[tuple[str, str, str, str]] = [("load", e, "", "entry point") for e in entries]
    source = [""]

    class _Todo(list):
        def append(self, item):  # every queued step remembers what queued it
            super().append((*item, source[0]) if len(item) == 3 else item)

        def extend(self, items):
            for item in items:
                self.append(item)

    todo = _Todo(todo)

    def whole(module: str) -> None:
        todo.append(("load", module, ""))
        todo.extend(("def", module, d) for d in mods[module].defs)

    def follow(module: str, names: set[str], attrs: set[tuple[str, str]], local: dict) -> None:
        m = mods[module]
        binding = {**m.bound, **local}
        for name in names:
            if name in m.defs:
                todo.append(("def", module, name))
            elif name in binding:
                target, attr = binding[name]
                todo.append(("load", target, ""))
                if attr is None:
                    whole(target)  # the module object itself is passed around: count all of it
                else:
                    todo.append(("def", target, attr))
        for name, attr in attrs:
            if name in binding and binding[name][1] is None:
                target = binding[name][0]
                todo.append(("load", target, ""))
                deeper = f"{target}.{attr}"
                if deeper in mods:  # package.submodule.something: cannot be followed further
                    whole(deeper)
                else:
                    todo.append(("def", target, attr))
            elif name in m.defs:
                todo.append(("def", module, name))
            elif name in binding:  # an imported class or function with an attribute taken from it
                target, got = binding[name]
                todo.append(("load", target, ""))
                todo.append(("def", target, got or ""))

    while todo:
        kind, module, name, by = todo.pop()
        if module not in mods:
            continue
        m = mods[module]
        if kind == "load":
            if module in loaded:
                continue
            loaded.add(module)
            source[0] = f"{module} (top level)"
            for package in _with_packages(module, known) - {module}:
                todo.append(("load", package, ""))
            for target in m.top_imports:
                todo.append(("load", target, ""))
            for target in m.star:
                whole(target)
            follow(module, *m.top_uses, {})
        else:
            if name in without.get(module, ()) or name in reached[module]:
                continue
            if name not in m.defs:
                source[0] = by
                if name in m.bound:  # a name the module re-exports from another one
                    target, attr = m.bound[name]
                    todo.append(("load", target, ""))
                    if attr is None:
                        whole(target)
                    else:
                        todo.append(("def", target, attr))
                continue
            reached[module].add(name)
            why[module][name] = by
            source[0] = f"{module}.{name}"
            names, attrs, local, lazy = m.uses[name]
            for target in lazy:
                todo.append(("load", target, ""))
            follow(module, names, attrs, local)
    return {"loaded": sorted(loaded), "reached": {k: sorted(v) for k, v in reached.items()}, "why": why,
            "defs": {k: sorted(m.defs) for k, m in mods.items()},
            "def_lines": {k: {d: n.end_lineno - n.lineno + 1 for d, n in m.defs.items()} for k, m in mods.items()}}


def _with_packages(name: str, known: dict[str, Path]) -> set[str]:
    """A module and the packages above it (importing core.narrate.script runs core/__init__ too)."""
    parts = name.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in known}


def imports_of(module: str, known: dict[str, Path]) -> set[str]:
    """Every project module this one imports, anywhere in the file."""
    path = known[module]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        for _name, target in _targets(node, module, path.name == "__init__.py", known):
            out |= _with_packages(target, known)
    out.discard(module)
    return out


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def app_slice(known: dict[str, Path], module: str = APP, roots: tuple[str, ...] = APP_ROOT_FUNCTIONS,
              without: tuple[str, ...] = APP_B2B_ONLY) -> dict:
    """app.py function by function. Returns:
    reached_functions / unreached_functions: its top-level functions and classes that can / cannot
    run when ``without`` is never entered; modules: {module: [names used by reached code]};
    only_unreached: {module: [the functions that use it]} for modules no reached code uses."""
    path = known[module]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defs: dict[str, ast.AST] = {}
    bound: dict[str, str] = {}  # a name app.py imports at the top -> the project module it comes from
    top_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
            for decorator in node.decorator_list:
                top_names |= _names(decorator)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, target in _targets(node, module, False, known):
                bound[name] = target
        else:
            top_names |= _names(node)  # constants and the `if __name__` block run in every mode

    uses: dict[str, set[str]] = {}  # function -> names it mentions
    lazy: dict[str, set[str]] = {}  # function -> modules it imports inside itself
    for name, node in defs.items():
        uses[name] = _names(node)
        lazy[name] = {t for n in ast.walk(node) for _b, t in _targets(n, module, False, known)}

    reached: set[str] = set()
    todo = [n for n in (*roots, *top_names) if n in defs and n not in without]
    while todo:
        name = todo.pop()
        if name in reached or name in without:
            continue
        reached.add(name)
        todo += [n for n in uses[name] if n in defs and n not in reached]

    modules: dict[str, set[str]] = {}
    for name in sorted(top_names):
        if name in bound:
            modules.setdefault(bound[name], set()).add(name)
    for fn in reached:
        for name in uses[fn]:
            if name in bound:
                modules.setdefault(bound[name], set()).add(name)
        for target in lazy[fn]:
            modules.setdefault(target, set())
    unreached = sorted(set(defs) - reached)
    only_unreached: dict[str, set[str]] = {}
    for fn in unreached:
        for target in {bound[n] for n in uses[fn] if n in bound} | lazy[fn]:
            if target not in modules:
                only_unreached.setdefault(target, set()).add(fn)
    never_used = sorted({t for t in bound.values() if t not in modules and t not in only_unreached})
    return {"reached_functions": sorted(reached), "unreached_functions": unreached,
            "modules": {m: sorted(v) for m, v in sorted(modules.items())},
            "only_unreached": {m: sorted(v) for m, v in sorted(only_unreached.items())},
            "imported_but_unused": never_used}


def reach(roots: set[str], graph: dict[str, set[str]], cut: set[str] = frozenset()) -> set[str]:
    """Every module reachable from ``roots``. ``cut``: modules whose own imports are not followed
    (the module itself still counts as reached)."""
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        if name not in cut:
            todo += list(graph[name] - seen)
    return seen


def tests_of(root: Path, known: dict[str, Path]) -> dict[str, set[str]]:
    """{test file: project modules it imports or names in a string (tools run as subprocesses)}."""
    out: dict[str, set[str]] = {}
    for path in sorted((root / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            for _name, target in _targets(node, "tests." + path.stem, False, known):
                found.add(target)
        for module, file in known.items():  # "tools/narrate.py" in a subprocess call, "core" / "x.py" path joins
            if module in ("core", "tools"):
                continue
            rel = str(file.relative_to(root))
            if rel in text or (len(file.relative_to(root).parts) > 1 and f'"{file.name}"' in text):
                found.add(module)
        out[path.name] = found
    return out


def lines(path: Path) -> int:
    return sum(1 for _ in path.open(encoding="utf-8"))


def analyse(root: Path = ROOT) -> dict:
    known = project_modules(root)
    graph = {m: imports_of(m, known) for m in known}
    sliced = app_slice(known)
    app_modules = set()
    for m in sliced["modules"]:
        app_modules |= _with_packages(m, known)
    roots = set(ENTRY_MODULES) | app_modules
    reached = reach(roots, graph) | {APP}
    # the same, if autonomous_worker (the outreach worker) is treated as a leaf: what is reached
    # only because the worker's own code imports it
    without_worker = reach(roots, graph, cut={"autonomous_worker"}) | {APP}
    importers = {m: sorted(k for k, v in graph.items() if m in v) for m in known}
    tests = tests_of(root, known)
    unreached = sorted(set(known) - reached)
    worker_only = sorted(reached - without_worker)

    def row(module: str) -> dict:
        mine = sorted(t for t, mods in tests.items() if module in mods)
        return {"module": module, "file": str(known[module].relative_to(root)), "lines": lines(known[module]),
                "imported_by": importers[module], "tests": mine,
                "app_functions": sliced["only_unreached"].get(module, [])}

    calls = call_reach(known)
    never_loaded = sorted(set(known) - set(calls["loaded"]) - set(unreached))
    nothing_called = sorted(m for m in calls["loaded"] if calls["defs"][m] and not calls["reached"][m]
                            and m not in unreached and m not in (APP, *ENTRY_MODULES))  # an entry point is run, not called
    partial = []
    for m in sorted(calls["loaded"]):
        dead = sorted(set(calls["defs"][m]) - set(calls["reached"][m]))
        if dead and calls["reached"][m] and m not in unreached:
            used = {d: calls["why"][m][d] for d in calls["reached"][m]}
            partial.append({"module": m, "file": str(known[m].relative_to(root)), "defs": len(calls["defs"][m]),
                            "unreached": dead, "unreached_lines": sum(calls["def_lines"][m][d] for d in dead),
                            "lines": lines(known[m]),
                            # what outside the module still mentions it: the functions that would need a new home
                            "used_from_outside": {d: by for d, by in sorted(used.items()) if not by.startswith(m + ".")
                                                  and by != f"{m} (top level)"},
                            "tests": sorted(t for t, mods in tests.items() if m in mods)})
    candidates = set(unreached) | set(never_loaded) | set(nothing_called)
    test_rows = []
    for name, mods in tests.items():
        mods = {m for m in mods if m not in ("core", "tools")}
        hit = sorted(mods & candidates)
        if hit:
            test_rows.append({"test": name, "candidates": hit, "also_uses_kept": sorted(mods - candidates)})
    return {"modules": len(known), "reached": sorted(reached), "unreached": [row(m) for m in unreached],
            "worker_only": [row(m) for m in worker_only], "app": sliced, "tests": test_rows,
            "never_loaded": [row(m) for m in never_loaded], "nothing_called": [row(m) for m in nothing_called],
            "partly_used": partial, "loaded": calls["loaded"],
            "lines_total": sum(lines(p) for p in known.values()),
            "graph": {m: sorted(v) for m, v in sorted(graph.items())}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = analyse()
    if args.json:
        json.dump(data, sys.stdout, indent=1)
        return
    app = data["app"]
    print(f"{data['modules']} project modules, {data['lines_total']:,} lines.")
    print(f"Reached from the gaming entry points: {len(data['reached'])}.")
    print(f"\nNot reached by any import ({len(data['unreached'])}, {sum(r['lines'] for r in data['unreached']):,} lines):")
    for r in data["unreached"]:
        print(f"  {r['file']:<34} {r['lines']:>5} lines  tests: {len(r['tests'])}  imported by: {', '.join(r['imported_by']) or '-'}")
    for title, key in (("Imported somewhere, but never loaded when calls are followed", "never_loaded"),
                       ("Loaded, but nothing in it is called", "nothing_called")):
        print(f"\n{title} ({len(data[key])}, {sum(r['lines'] for r in data[key]):,} lines):")
        for r in data[key]:
            print(f"  {r['file']:<34} {r['lines']:>5} lines  tests: {len(r['tests'])}  imported by: {', '.join(r['imported_by']) or '-'}")
    print(f"\nPartly used ({len(data['partly_used'])} files; the ones with 150+ unreached lines):")
    for r in sorted(data["partly_used"], key=lambda r: -r["unreached_lines"]):
        if r["unreached_lines"] >= 150:
            print(f"  {r['file']:<34} {len(r['unreached']):>3} of {r['defs']:>3} functions/classes unreached, {r['unreached_lines']:>5} lines")
    print(f"\napp.py: {len(app['reached_functions'])} functions and classes can run in gaming mode, "
          f"{len(app['unreached_functions'])} cannot (B2B only).")


if __name__ == "__main__":
    main()
