"""Dependency-free test runner: ``python tests/run_tests.py``. The tests are plain functions, so ``pytest`` also runs them."""
import importlib.util
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent), str(ROOT)]
files = sorted(p for p in ROOT.rglob("test_*.py"))
only = sys.argv[1] if len(sys.argv) > 1 else ""
passed, failed, t0 = 0, [], time.time()
for f in files:
    if only and only not in str(f):
        continue
    spec = importlib.util.spec_from_file_location(f.stem + str(abs(hash(str(f)))), f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in dir(mod):
        if name.startswith("test_") and callable(getattr(mod, name)):
            try:
                getattr(mod, name)()
                passed += 1
            except Exception:
                failed.append(f"{f.name}::{name}\n{traceback.format_exc(limit=4)}")
print(f"{passed} passed, {len(failed)} failed in {time.time() - t0:.1f}s")
for x in failed:
    print("FAIL", x)
sys.exit(1 if failed else 0)
