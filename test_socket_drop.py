"""
Test socket drop behavior.
Verifies that when a feeder drops connection abnormally, the reconciler
properly detects and marks the link state.
"""

import sys
import subprocess
import time
import socket
import json
import requests

def test_socket_drop():
    http_port = 8011
    tcp_port = 9011
    
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
        for _ in range(20):
            try:
                r = requests.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
                
        # Connect client socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", tcp_port))
        
        # Verify state is "connected"
        time.sleep(0.3)
        res = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        assert res["connection_state"] == "connected"
        
        # Send one chunk
        chunk_pkt = {
            "type": "chunk",
            "chunk_id": 0,
            "window_start": 0.0,
            "window_end": 30.0,
            "is_failure": False,
            "words": [{"text": "Hello", "start": 0.5, "end": 0.8}]
        }
        sock.sendall((json.dumps(chunk_pkt) + "\n").encode("utf-8"))
        time.sleep(0.3)
        res = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        assert res["connection_state"] == "receiving"
        
        # Abruptly close socket with RST (force drop)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00')
        sock.close()
        time.sleep(0.5)
        
        # Check connection state after drop
        res = requests.get(f"http://127.0.0.1:{http_port}/api/status").json()
        print(f"[Drop Test] Connection state after drop: {res['connection_state']}")
        assert res["connection_state"] in ("dropped", "done", "disconnected")
        print("[Drop Test] Abrupt disconnection handled cleanly!")
        
    finally:
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    test_socket_drop()
