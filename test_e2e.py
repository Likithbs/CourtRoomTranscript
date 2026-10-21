"""
End-to-End System Test.
Tests the real multi-process socket pipeline:
1. Starts the FastAPI server & TCP socket server on test ports.
2. Runs the real Feeder process over TCP.
3. Verifies full transmission, real-time seam resolution, and REST API queries.
"""

import sys
import subprocess
import time
import requests
import pytest

def test_full_pipeline_e2e():
    http_port = 8008
    tcp_port = 9008
    
    # 1. Start Server in background process
    server_cmd = [
        sys.executable, "-c",
        f"""
import uvicorn, os
from server import app, state
state.tcp_port = {tcp_port}
uvicorn.run(app, host='127.0.0.1', port={http_port}, log_level='error')
"""
    ]
    
    server_proc = subprocess.Popen(server_cmd)
    
    try:
        # Wait for HTTP server to become responsive
        server_ready = False
        for _ in range(20):
            try:
                r = requests.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)
                if r.status_code == 200:
                    server_ready = True
                    break
            except Exception:
                time.sleep(0.3)
                
        assert server_ready, "Server failed to start!"
        
        # Check initial status
        r = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        assert r["connection_state"] == "disconnected"
        assert r["stats"]["chunks_successful"] == 0
        
        # 2. Run Feeder process over real TCP socket
        feeder_cmd = [
            sys.executable, "feeder.py",
            "--host", "127.0.0.1",
            "--port", str(tcp_port),
            "--delay", "0.002"  # Fast delivery for test
        ]
        feeder_proc = subprocess.run(feeder_cmd, capture_output=True, text=True)
        assert feeder_proc.returncode == 0, f"Feeder failed: {feeder_proc.stderr}"
        
        # Give server a moment to finish processing
        time.sleep(0.8)
        
        # 3. Verify Server State via REST API
        status_res = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        assert status_res["connection_state"] == "done"
        stats = status_res["stats"]
        
        print("\n[E2E] Final Status from REST API:")
        print(f"  Connection State: {status_res['connection_state']}")
        print(f"  Chunks Successful: {stats['chunks_successful']} / {stats['expected_chunks']}")
        print(f"  Seams Resolved: {stats['seams_resolved']} / {stats['expected_seams']}")
        print(f"  Duplicates Filtered: {stats['duplicates_ignored']}")
        print(f"  Failures Handled: {stats['chunks_failed']}")
        print(f"  Total Reconciled Words: {stats['total_reconciled_words']}")
        
        assert stats["chunks_successful"] == 96
        assert stats["seams_resolved"] == 95
        assert stats["chunks_complete_pct"] == 100.0
        assert stats["seams_complete_pct"] == 100.0
        assert stats["total_reconciled_words"] > 4500
        
        # 4. Check Transcript API
        transcript_res = requests.get(f"http://127.0.0.1:{http_port}/api/transcript").json()
        words = transcript_res["words"]
        assert len(words) == stats["total_reconciled_words"]
        
        # Verify timestamps are monotonically non-decreasing
        for i in range(len(words) - 1):
            assert words[i]["start"] <= words[i+1]["start"] + 0.05, f"Timestamps out of order at index {i}: {words[i]['start']} > {words[i+1]['start']}"
            
        print("[E2E] Monotonic timestamp ordering verified!")
        print("[E2E] End-to-End Pipeline test PASSED!")
        
    finally:
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    test_full_pipeline_e2e()
