"""A stricter project check for the demo: run the tests, and require type annotations on every function.

Used as the board's validation command (`--validate-cmd "python3 strict_check.py"`) to see what an agent does when
`submit_task` answers `rejected` for a rule the task text never mentioned.
"""
import ast
import subprocess
import sys
from pathlib import Path

problems = []
for path in sorted(Path(".").glob("*.py")):
    if path.name.startswith("test_") or path.name == "registry.py":
        continue
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as e:
        problems.append(f"{path.name}: syntax error: {e}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            args = [a for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
            missing = [a.arg for a in args if a.annotation is None]
            if missing or node.returns is None:
                problems.append(f"{path.name}: function {node.name} needs type annotations on every parameter "
                                f"and on its return value (missing: {', '.join(missing + (['return'] if node.returns is None else []))})")
if problems:
    print("project rules violated:\n" + "\n".join(problems))
    sys.exit(1)
run = subprocess.run([sys.executable, "-m", "unittest", "discover"], capture_output=True, text=True)
if run.returncode != 0:
    print((run.stdout + run.stderr)[-1500:])
    sys.exit(1)
