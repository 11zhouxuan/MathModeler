"""Test: verify write_files + execute workflow for the streaming code execution.

Tests two approaches:
1. write_files with relative path -> execute reads it
2. write_files with /tmp/ absolute path -> execute reads it

Run:
    cd /efs/projects/aws_summit_2026/mathmodeler
    PYTHONPATH=common uv run --with bedrock-agentcore python scripts/test_ci_write_execute.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

from mm_common.runners import CodeInterpreterClient


def main():
    print("=" * 60)
    print("TEST: CI write_files + execute file visibility")
    print("=" * 60)

    ci = CodeInterpreterClient(region="us-west-2")
    ci.start()
    print("✓ CI session started\n")

    try:
        # Test 1: write_files with RELATIVE path
        print("[Test 1] write_files(path='_user_code.py') ...")
        result = ci.write_files([{"path": "_user_code.py", "text": "print('hello from relative path')"}])
        print(f"  write_files result: {result}")

        print("  Now execute: exec(open('_user_code.py').read()) ...")
        result = ci.execute("exec(open('_user_code.py').read())")
        print(f"  execute result ok={result.get('ok')}, stdout={result.get('stdout','').strip()!r}")
        if result.get("ok"):
            print("  ✓ Test 1 PASSED: relative path works\n")
        else:
            print(f"  ✗ Test 1 FAILED: {result.get('stderr')}\n")

        # Test 2: write_files with /tmp/ absolute path
        print("[Test 2] write_files(path='/tmp/user_code.py') ...")
        try:
            result = ci.write_files([{"path": "/tmp/user_code.py", "text": "print('hello from /tmp')"}])
            print(f"  write_files result: {result}")
            stderr = result.get("stderr", "")
            if "path traversal" in str(stderr).lower() or "invalid" in str(stderr).lower():
                print("  → write_files REJECTED /tmp/ path (security filter)")
                print("  ✗ Test 2: /tmp/ path blocked by SDK\n")
            else:
                print("  Now execute: exec(open('/tmp/user_code.py').read()) ...")
                result = ci.execute("exec(open('/tmp/user_code.py').read())")
                print(f"  execute result ok={result.get('ok')}, stdout={result.get('stdout','').strip()!r}")
                if result.get("ok"):
                    print("  ✓ Test 2 PASSED: /tmp/ path works\n")
                else:
                    print(f"  ✗ Test 2 FAILED: {result.get('stderr')}\n")
        except Exception as e:
            print(f"  ✗ Test 2 EXCEPTION: {e}\n")

        # Test 3: Full wrapper pattern with relative paths
        print("[Test 3] Full wrapper pattern (relative paths) ...")
        user_code = "import math\nresult = math.factorial(10)\nprint(f'10! = {result}', flush=True)"
        ci.write_files([{"path": "_user_code.py", "text": user_code}])
        
        wrapper = (
            "import sys, io\n"
            "class _Tee(io.TextIOBase):\n"
            "    def __init__(self, orig, log):\n"
            "        self._orig = orig\n"
            "        self._log = log\n"
            "    def write(self, s):\n"
            "        self._orig.write(s)\n"
            "        self._log.write(s)\n"
            "        self._log.flush()\n"
            "        return len(s)\n"
            "    def flush(self):\n"
            "        self._orig.flush()\n"
            "        self._log.flush()\n"
            "_log_f = open('_run.log', 'w')\n"
            "sys.stdout = _Tee(sys.stdout, _log_f)\n"
            "try:\n"
            "    exec(open('_user_code.py').read())\n"
            "finally:\n"
            "    sys.stdout = sys.stdout._orig\n"
            "    _log_f.close()\n"
            "    open('_run_done', 'w').write('1')\n"
        )
        result = ci.execute(wrapper)
        stdout = result.get("stdout", "").strip()
        print(f"  execute result ok={result.get('ok')}, stdout={stdout!r}")
        if result.get("ok") and "3628800" in stdout:
            print("  ✓ Test 3 PASSED: full wrapper works with relative paths\n")
        else:
            print(f"  ✗ Test 3 FAILED: stderr={result.get('stderr')}\n")

        # Test 4: Verify log file was created
        print("[Test 4] Verify _run.log was written ...")
        result = ci.execute("print(open('_run.log').read())")
        log_content = result.get("stdout", "").strip()
        print(f"  _run.log content: {log_content!r}")
        if "3628800" in log_content:
            print("  ✓ Test 4 PASSED: log file captured output\n")
        else:
            print("  ✗ Test 4 FAILED\n")

    finally:
        ci.stop()
        print("CI session stopped.")


if __name__ == "__main__":
    main()
