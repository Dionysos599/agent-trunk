"""Check a finished result of the demo: python3 check.py DIR

DIR is a folder holding the files of main (`agenttrunk export DIR`). The demo is done when every module is
written, its tests pass, and the two shared files list all ten features.
"""

import re
import subprocess
import sys
from pathlib import Path

MODULES = ["prices", "tax", "cart", "inventory", "shipping", "coupons", "receipt", "search", "validators", "slugs"]


def main(root: Path) -> int:
    problems = []
    for m in MODULES:
        src = root / f"{m}.py"
        if not src.exists():
            problems.append(f"{m}.py is missing")
        elif "NotImplementedError" in src.read_text():
            problems.append(f"{m}.py is still the stub")
        if not (root / f"test_{m}.py").exists():
            problems.append(f"test_{m}.py is missing")
    reg = root / "registry.py"
    features = []
    if reg.exists():
        ns = {}
        try:
            exec(reg.read_text(), ns)                       # noqa: S102  (our own demo file)
            features = list(ns.get("FEATURES", []))
        except Exception as e:                              # noqa: BLE001
            problems.append(f"registry.py does not run: {e}")
    for m in MODULES:
        if features.count(m) != 1:
            problems.append(f"registry.py lists {m!r} {features.count(m)} times (want 1)")
    log = (root / "CHANGELOG.md").read_text() if (root / "CHANGELOG.md").exists() else ""
    for m in MODULES:
        n = len(re.findall(rf"^- {m}:", log, re.M))
        if n != 1:
            problems.append(f"CHANGELOG.md has {n} lines for {m} (want 1)")
    if re.search(r"^(<<<<<<<|=======|>>>>>>>)", (reg.read_text() if reg.exists() else "") + log, re.M):
        problems.append("conflict markers were left in registry.py or CHANGELOG.md")
    run = subprocess.run([sys.executable, "-m", "unittest", "discover"], cwd=root, capture_output=True, text=True)
    tail = (run.stderr or run.stdout).strip().splitlines()[-3:]
    if run.returncode != 0:
        problems.append("the tests fail: " + " | ".join(tail))
    ran = re.search(r"Ran (\d+) tests", run.stderr)
    count = int(ran.group(1)) if ran else 0
    print(f"modules written: {sum(1 for m in MODULES if (root / f'{m}.py').exists() and 'NotImplementedError' not in (root / f'{m}.py').read_text())}/10   "
          f"features registered: {len(set(features) & set(MODULES))}/10   tests run: {count}")
    if problems:
        print("NOT DONE:")
        for p in problems:
            print("  -", p)
        return 1
    print("ALL DONE: ten features, all tests pass, nothing lost from the shared files.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
