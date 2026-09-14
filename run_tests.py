#!/usr/bin/env python
"""
Single entry point for the entire automated test suite -- both halves of
it (see TESTING.md for why there are two):

  1. The 16 legacy test_*.py scripts at the repo root -- each a plain,
     directly-executable script (asserts + prints), run as its own
     subprocess exactly like `python test_x.py` would.
  2. The tests/ pytest suite -- dangerous-false-conclusion tests,
     conflicting-data tests, backend contract tests, and (if Playwright
     is installed) real-browser frontend e2e tests.

Usage:
    python run_tests.py            # everything
    python run_tests.py --fast     # skip the Playwright e2e file (slower, needs a browser binary)

Exits non-zero if anything failed, so this is CI-friendly as a single
pass/fail gate.
"""
import argparse
import glob
import subprocess
import sys
import time

REPO_ROOT = __file__.rsplit("run_tests.py", 1)[0] or "."


def run_legacy_scripts():
    print("\n" + "=" * 78)
    print("LEGACY SCRIPTS (test_*.py at repo root)")
    print("=" * 78)
    results = []
    for path in sorted(glob.glob(f"{REPO_ROOT}/test_*.py")):
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        start = time.time()
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
        elapsed = time.time() - start
        out = proc.stdout + proc.stderr
        failed = proc.returncode != 0 or "traceback" in out.lower() or "assertionerror" in out.lower()
        results.append((name, not failed, elapsed))
        status = "PASS" if not failed else "FAIL"
        print(f"  [{status}] {name} ({elapsed:.1f}s)")
        if failed:
            print("-" * 78)
            print(out[-4000:])
            print("-" * 78)
    return results


def run_pytest_suite(fast: bool):
    print("\n" + "=" * 78)
    print("PYTEST SUITE (tests/)")
    print("=" * 78)
    args = [sys.executable, "-m", "pytest", f"{REPO_ROOT}/tests", "-v"]
    if fast:
        args += ["--ignore", f"{REPO_ROOT}/tests/test_frontend_e2e.py"]
    proc = subprocess.run(args)
    return proc.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="skip the Playwright browser e2e tests")
    args = parser.parse_args()

    legacy_results = run_legacy_scripts()
    pytest_ok = run_pytest_suite(fast=args.fast)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    legacy_failed = [n for n, ok, _ in legacy_results if not ok]
    for name, ok, elapsed in legacy_results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"  [{'PASS' if pytest_ok else 'FAIL'}] pytest suite (tests/)")

    total_ok = not legacy_failed and pytest_ok
    print()
    if total_ok:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED:")
        for name in legacy_failed:
            print(f"  - {name}")
        if not pytest_ok:
            print("  - pytest suite (see output above)")
    sys.exit(0 if total_ok else 1)


if __name__ == "__main__":
    main()
