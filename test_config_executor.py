"""
Test bias switch config endpoint offloading to executor.
"""

import sys
import subprocess
import time
import requests
import pytest


def test_bias_config_executor():
    http_port = 8023
    tcp_port = 9023

    server_cmd = [
        sys.executable, "-c",
        f"""
import uvicorn
from server import app, state
state.tcp_port = {tcp_port}
uvicorn.run(app, host='127.0.0.1', port={http_port}, log_level='error')
"""
    ]

    server_proc = subprocess.Popen(server_cmd)

    try:
        # Wait for server
        for _ in range(25):
            try:
                r = requests.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)

        # Switch bias policy via REST API
        res = requests.post(
            f"http://127.0.0.1:{http_port}/api/config",
            json={"bias": "anti-drop"}
        )
        assert res.status_code == 200
        assert res.json()["bias"] == "anti-drop"

        # Switch back to anti-duplication
        res2 = requests.post(
            f"http://127.0.0.1:{http_port}/api/config",
            json={"bias": "anti-duplication"}
        )
        assert res2.status_code == 200
        assert res2.json()["bias"] == "anti-duplication"

    finally:
        server_proc.terminate()
        server_proc.wait()
