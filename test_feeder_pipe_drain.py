"""
Test feeder process output draining and status log surfacing.
"""

import sys
import subprocess
import time
import requests
import pytest


def test_feeder_stdout_drained_without_deadlock():
    http_port = 8021
    tcp_port = 9021

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
        # Wait for HTTP server
        for _ in range(25):
            try:
                r = requests.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)

        # Start feeder via REST API with 0 delay (high throughput, max chance of pipe overflow if not drained)
        res = requests.post(f"http://127.0.0.1:{http_port}/api/feeder/start", json={"delay": 0.001}).json()
        assert res["status"] in ("started", "already_running")

        # Wait for feeder to stream
        time.sleep(2.5)

        # Fetch status
        status = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        assert status["connection_state"] == "done"
        
        # Verify logs were captured and drained from pipe
        feeder_logs = status.get("feeder_logs", [])
        assert len(feeder_logs) > 0
        print(f"Captured {len(feeder_logs)} feeder log lines successfully.")
        assert any("Sent packet" in line or "stream_manifest" in line for line in feeder_logs)

    finally:
        server_proc.terminate()
        server_proc.wait()
