"""The phone's JS signature must match the server's Python HMAC byte for byte."""
import hashlib
import json
import shutil
import subprocess

import pytest

from server.config import ROOT
from server.security import sign_scan

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_js_hmac_matches_python():
    key = "ab" * 32
    cases = [("T00001", 1, "8901000000011", 1760000000000), ("T00042", 17, "8901000000059", 5)]
    script = (
        f"const c = require({json.dumps(str(ROOT / 'web' / 'sha256.js'))});"
        f"const cases = {json.dumps(cases)};"
        f"console.log(JSON.stringify({{sha: c.sha256Hex('abc'), long: c.sha256Hex('x'.repeat(200)),"
        f" macs: cases.map(([s,q,b,t]) => c.hmacHex({json.dumps(key)}, `${{s}}|${{q}}|${{b}}|${{t}}`))}}));"
    )
    out = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["sha"] == hashlib.sha256(b"abc").hexdigest()
    assert out["long"] == hashlib.sha256(b"x" * 200).hexdigest()
    assert out["macs"] == [sign_scan(key, *c) for c in cases]
