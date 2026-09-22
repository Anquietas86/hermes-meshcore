"""Dependency-free focused test runner for the MeshCore plugin."""

import importlib
import inspect
import sys
import traceback


TEST_MODULES = (
    "test_profile_scope",
    "test_security",
    "test_security_enhanced",
    "test_integration",
    "test_regressions",
)


def main() -> int:
    failures = []
    count = 0
    for module_name in TEST_MODULES:
        module = importlib.import_module(module_name)
        tests = [
            (name, function)
            for name, function in inspect.getmembers(module, inspect.isfunction)
            if name.startswith("test_") and function.__module__ == module_name
        ]
        for name, function in tests:
            count += 1
            qualified_name = f"{module_name}.{name}"
            try:
                function()
            except Exception:
                failures.append(qualified_name)
                print(f"FAIL {qualified_name}", file=sys.stderr)
                traceback.print_exc()
            else:
                print(f"PASS {qualified_name}")

    if failures:
        print(f"\n{len(failures)} of {count} tests failed", file=sys.stderr)
        return 1
    print(f"\nAll {count} focused tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
