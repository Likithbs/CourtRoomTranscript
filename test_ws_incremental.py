"""
Test WebSocket incremental diffs, sequence tracking, and resync.
"""

import sys
import subprocess
import time
import json
import socket
import pytest
import requests
import websockets
import asyncio


@pytest.mark.asyncio
async def test_websocket_incremental_diff_and_resync():
    http_port = 8019
    tcp_port = 9019
    
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
                await asyncio.sleep(0.2)
                
        # Connect WebSocket client
        ws_url = f"ws://127.0.0.1:{http_port}/ws"
        async with websockets.connect(ws_url) as ws:
            # 1. Verify init event
            init_msg = json.loads(await ws.recv())
            assert init_msg["event"] == "init"
            assert "seq" in init_msg
            last_seq = init_msg["seq"]
            
            # Connect TCP feeder socket
            feeder_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            feeder_sock.connect(("127.0.0.1", tcp_port))
            
            # Consume connection_state message on WS
            conn_msg = json.loads(await ws.recv())
            assert conn_msg["event"] == "connection_state"
            assert conn_msg["seq"] == last_seq + 1
            last_seq = conn_msg["seq"]
            
            # Send stream_manifest header
            manifest = {
                "type": "stream_manifest",
                "total_packets": 3,
                "expected_chunks": 2
            }
            feeder_sock.sendall((json.dumps(manifest) + "\n").encode("utf-8"))
            await asyncio.sleep(0.1)
            
            # Send chunk 0
            c0 = {
                "type": "chunk",
                "chunk_id": 0,
                "window_start": 0.0,
                "window_end": 30.0,
                "is_failure": False,
                "words": [
                    {"text": "Court", "start": 1.0, "end": 1.5},
                    {"text": "is", "start": 1.6, "end": 1.8},
                    {"text": "now", "start": 1.9, "end": 2.1},
                    {"text": "in", "start": 26.0, "end": 26.4},
                ]
            }
            feeder_sock.sendall((json.dumps(c0) + "\n").encode("utf-8"))
            
            # May receive connection_state "receiving" first
            msg1 = json.loads(await ws.recv())
            if msg1["event"] == "connection_state":
                assert msg1["seq"] == last_seq + 1
                last_seq = msg1["seq"]
                msg1 = json.loads(await ws.recv())
                
            # Verify packet_ingested event
            assert msg1["event"] == "packet_ingested"
            assert msg1["seq"] == last_seq + 1
            last_seq = msg1["seq"]
            assert "new_words" in msg1["data"]
            assert len(msg1["data"]["new_words"]) == 4
            assert [w["text"] for w in msg1["data"]["new_words"]] == ["Court", "is", "now", "in"]
            assert msg1["data"]["stats"]["expected_chunks"] == 2
            
            # Send chunk 1 with overlapping "in"
            c1 = {
                "type": "chunk",
                "chunk_id": 1,
                "window_start": 25.0,
                "window_end": 55.0,
                "is_failure": False,
                "words": [
                    {"text": "in", "start": 26.05, "end": 26.45},
                    {"text": "session", "start": 32.0, "end": 32.5},
                ]
            }
            feeder_sock.sendall((json.dumps(c1) + "\n").encode("utf-8"))
            
            msg2 = json.loads(await ws.recv())
            assert msg2["event"] == "packet_ingested"
            assert msg2["seq"] == last_seq + 1
            last_seq = msg2["seq"]
            # Incremental diff should ONLY contain the newly added word "session"
            assert len(msg2["data"]["new_words"]) == 1
            assert [w["text"] for w in msg2["data"]["new_words"]] == ["session"]
            assert msg2["data"]["stats"]["total_reconciled_words"] == 5
            
            # Send duplicate chunk 0
            feeder_sock.sendall((json.dumps(c0) + "\n").encode("utf-8"))
            msg_dup = json.loads(await ws.recv())
            assert msg_dup["event"] == "packet_ingested"
            assert msg_dup["seq"] == last_seq + 1
            last_seq = msg_dup["seq"]
            # No new words for duplicate
            assert len(msg_dup["data"]["new_words"]) == 0
            
            # 2. Test Resync functionality
            await ws.send(json.dumps({"action": "resync"}))
            resync_msg = json.loads(await ws.recv())
            assert resync_msg["event"] == "resync"
            assert len(resync_msg["data"]["transcript"]) == 5
            all_texts = [w["text"] for w in resync_msg["data"]["transcript"]]
            assert all_texts == ["Court", "is", "now", "in", "session"]
            
            feeder_sock.close()
    finally:
        server_proc.terminate()
        server_proc.wait()
