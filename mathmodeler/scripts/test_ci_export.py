"""Minimal test: verify we can export binary files from Code Interpreter sandbox.

Steps:
1. Start a CI session
2. Generate a small PNG inside the sandbox (matplotlib)
3. Use executeCode to base64-encode the PNG and print to stdout
4. Decode the stdout locally and write to a local file
5. Verify the local file is a valid PNG (check magic bytes)

Run:
    cd /efs/projects/aws_summit_2026/mathmodeler
    PYTHONPATH=common uv run --with bedrock-agentcore python scripts/test_ci_export.py
"""
import base64
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

from mm_common.runners import CodeInterpreterClient, _parse_ci_result


def main():
    print("=" * 60)
    print("TEST: Code Interpreter binary file export via executeCode")
    print("=" * 60)

    # 1. Start CI session
    print("\n[1] Starting Code Interpreter session...")
    ci = CodeInterpreterClient(region="us-west-2")
    ci.start()
    print("    ✓ Session started")

    try:
        # 2. Generate a small PNG in the sandbox
        print("\n[2] Generating test PNG in sandbox...")
        gen_code = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2*np.pi, 50)
plt.figure(figsize=(4,3))
plt.plot(x, np.sin(x), 'b-', lw=2)
plt.title('Test Plot')
plt.savefig('test_plot.png', dpi=72)
plt.close()

import os
size = os.path.getsize('test_plot.png')
print(f"Generated test_plot.png ({size} bytes)")
"""
        result = ci.execute(gen_code)
        print(f"    stdout: {result.get('stdout', '').strip()}")
        print(f"    ok: {result.get('ok')}")
        if not result.get("ok"):
            print(f"    stderr: {result.get('stderr')}")
            return

        # 3. Read the PNG via executeCode + base64
        print("\n[3] Reading PNG via executeCode (base64 in stdout)...")
        read_code = """import base64
with open('test_plot.png', 'rb') as f:
    data = f.read()
print(base64.b64encode(data).decode())
"""
        result = ci.execute(read_code)
        stdout = result.get("stdout", "").strip()
        print(f"    ok: {result.get('ok')}")
        print(f"    stdout length: {len(stdout)} chars")

        if not stdout:
            print("    ❌ FAILED: stdout is empty!")
            return

        # 4. Decode and write locally
        print("\n[4] Decoding base64 and writing to local temp file...")
        try:
            raw_bytes = base64.b64decode(stdout)
        except Exception as e:
            print(f"    ❌ FAILED: base64 decode error: {e}")
            print(f"    First 100 chars of stdout: {stdout[:100]}")
            return

        out_path = "/tmp/ci_test_plot.png"
        with open(out_path, "wb") as f:
            f.write(raw_bytes)
        print(f"    Written {len(raw_bytes)} bytes to {out_path}")

        # 5. Verify PNG magic bytes
        print("\n[5] Verifying PNG magic bytes...")
        PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
        if raw_bytes[:8] == PNG_MAGIC:
            print(f"    ✓ VALID PNG! ({len(raw_bytes)} bytes)")
            print(f"\n{'=' * 60}")
            print("TEST PASSED: executeCode + base64 stdout can export binary files")
            print(f"{'=' * 60}")
        else:
            print(f"    ❌ NOT a valid PNG. First 16 bytes: {raw_bytes[:16].hex()}")

        # Also test readFiles for comparison
        print("\n[BONUS] Testing ci.read_files(['test_plot.png'])...")
        rf_result = ci.read_files(["test_plot.png"])
        rf_stdout = rf_result.get("stdout", "")
        print(f"    read_files stdout length: {len(rf_stdout)} chars")
        print(f"    read_files ok: {rf_result.get('ok')}")
        if not rf_stdout:
            print("    → Confirmed: readFiles returns EMPTY for binary files (SDK limitation)")

    finally:
        print("\n[cleanup] Stopping CI session...")
        ci.stop()
        print("    Done.")


if __name__ == "__main__":
    main()
