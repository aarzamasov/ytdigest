#!/usr/bin/env python3
"""Run every test module in its own interpreter and summarise. Dependency-free alternative to pytest.

python tests/run_all.py            # all modules
python tests/run_all.py units cli  # modules whose file name contains any of the words
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    modules = sorted(p for p in TESTS.glob("*_test.py")) + sorted(p for p in TESTS.glob("test_*.py"))
    if argv:
        modules = [m for m in modules if any(word in m.name for word in argv)]
    failures = []
    started = time.time()
    for module in modules:
        print(f"\n== {module.name} ==")
        result = subprocess.run([sys.executable, str(module)], cwd=str(TESTS.parent))
        if result.returncode != 0:
            failures.append(module.name)
    print(f"\n{len(modules) - len(failures)}/{len(modules)} modules passed in {time.time() - started:.1f}s")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
