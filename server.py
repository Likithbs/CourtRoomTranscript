"""
FastAPI Reconciler Service.
Hosts:
1. An Async TCP Socket Server (listening on 127.0.0.1:9000) that accepts chunk streams from the feeder process.
2. Real-time WebSocket broadcasting connection state, metrics, and reconciled transcript.
3. REST endpoints to drive, inspect, configure, and reset the reconciler.
4. Static file serving for the Reviewer browser page.
"""

import asyncio
import json
import os
import sys
import subprocess
import threading
from contextlib import asynccontextmanager
from typing import Set, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from reconciler import TranscriptReconciler

# Global Application State
class ServiceState:
    def __init__(self):
        self.tcp_host = "127.0.0.1"
        self.tcp_port = 9000
        self.connection_state = "disconnected"  # "disconnected", "connected", "receiving", "done", "dropped"
        self.active_feeder_ip = None
        self.reconciler = TranscriptReconciler(bias="anti-duplication")
        self.websocket_clients: Set[WebSocket] = set()
        self.tcp_server = None
        self.feeder_subprocess: Optional[subprocess.Popen] = None
        self.feeder_pid: Optional[int] = None
        self.last_event_message = "Ready. Waiting for feeder to connect."
        self.ws_sequence: int = 0
        self.broadcast_word_keys: Set[Tuple[int, float, float, str]] = set()
        self.expected_chunks: Optional[int] = None
        self.highest_chunk_id: int = -1
        self.total_packets_sent: Optional[int] = None
        self.feeder_logs: List[str] = []

    def add_feeder_log(self, line: str):
        self.feeder_logs.append(line)
        if len(self.feeder_logs) > 200:
            self.feeder_logs.pop(0)

state = ServiceState()


async def broadcast_ws(event_type: str, data: Dict[str, Any]):
    """Broadcasts a sequence-numbered event message to all connected browser clients."""
    if not state.websocket_clients:
        return
    state.ws_sequence += 1
    message = json.dumps({"event": event_type, "seq": state.ws_sequence, "data": data})
    disconnected = set()
    for ws in state.websocket_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    for ws in disconnected:
        state.websocket_clients.discard(ws)


async def set_connection_state(new_state: str, message: Optional[str] = None):
    """Updates connection state and broadcasts to browser."""
    state.connection_state = new_state
    if message:
        state.last_event_message = message
    print(f"[TCP Server] Connection state: {new_state} - {state.last_event_message}")
    await broadcast_ws("connection_state", {
        "state": state.connection_state,
        "message": state.last_event_message,
        "stats": state.reconciler.get_stats(expected_chunks=state.expected_chunks)
    })


async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """
    Handles an incoming TCP connection from the feeder process.
    Reads newline-delimited JSON chunks.
    """
    peer = writer.get_extra_info('peername')
    state.active_feeder_ip = f"{peer[0]}:{peer[1]}"
    await set_connection_state("connected", f"Feeder connected from {state.active_feeder_ip}")
    
    received_any = False
    
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError as val_err:
                print(f"[TCP Server] Error: Packet exceeded maximum line buffer limit: {val_err}")
                await set_connection_state("dropped", f"Packet framing limit exceeded: {val_err}")
                break

            if not line:
                # Socket EOF (connection closed by feeder)
                break
                
            line_str = line.decode('utf-8').strip()
            if not line_str:
                continue
                
            try:
                packet = json.loads(line_str)
            except json.JSONDecodeError as err:
                print(f"[TCP Server] JSON decode error: {err}")
                continue
                
            packet_type = packet.get("type", "chunk")
            
            if packet_type == "stream_manifest":
                state.expected_chunks = packet.get("expected_chunks")
                state.total_packets_sent = packet.get("total_packets")
                state.reconciler.expected_chunks = state.expected_chunks
                print(f"[TCP Server] Received stream_manifest: expected_chunks={state.expected_chunks}, total_packets={state.total_packets_sent}")
                continue

            if packet_type == "stream_end":
                print(f"[TCP Server] Received stream_end from feeder.")
                received_any = True
                state.total_packets_sent = packet.get("total_sent")
                if state.expected_chunks is None:
                    inferred = max(state.highest_chunk_id + 1, len(state.reconciler.received_chunks))
                    state.expected_chunks = inferred
                    state.reconciler.expected_chunks = inferred
                await set_connection_state("done", f"Stream finished. All chunks received and reconciled.")
                break
                
            if not received_any:
                received_any = True
                await set_connection_state("receiving", "Receiving and reconciling chunk stream...")
                
            cid = packet.get("chunk_id")
            if cid is not None and cid > state.highest_chunk_id:
                state.highest_chunk_id = cid
                if state.expected_chunks is None:
                    state.reconciler.expected_chunks = state.highest_chunk_id + 1

            # Ingest packet into reconciler
            result = state.reconciler.ingest_packet(packet)
            
            # Assemble/cache full transcript once per packet
            full_transcript = state.reconciler.build_full_transcript()
            # Retrieve stats from cache in O(1)
            stats = state.reconciler.get_stats(expected_chunks=state.expected_chunks)
            
            # Incremental diff: only newly added words since last broadcast
            new_words = []
            for w in full_transcript:
                is_seen = False
                if w.word_index is not None and (w.chunk_id, w.word_index) in state.broadcast_word_keys:
                    is_seen = True
                elif w.alt_chunk_id is not None and w.alt_word_index is not None and (w.alt_chunk_id, w.alt_word_index) in state.broadcast_word_keys:
                    is_seen = True
                elif (w.chunk_id, w.text, round(w.start, 2)) in state.broadcast_word_keys:
                    is_seen = True

                if not is_seen:
                    if w.word_index is not None:
                        state.broadcast_word_keys.add((w.chunk_id, w.word_index))
                    if w.alt_chunk_id is not None and w.alt_word_index is not None:
                        state.broadcast_word_keys.add((w.alt_chunk_id, w.alt_word_index))
                    state.broadcast_word_keys.add((w.chunk_id, w.text, round(w.start, 2)))
                    new_words.append(w)

            # Broadcast chunk event with incremental diff
            await broadcast_ws("packet_ingested", {
                "packet_result": result,
                "stats": stats,
                "new_words": [w.to_dict() for w in new_words],
                "resolved_seams": [s.to_dict() for s in state.reconciler.resolved_seams.values()]
            })
            
        if state.connection_state != "done":
            if received_any:
                if state.expected_chunks is None:
                    inferred = max(state.highest_chunk_id + 1, len(state.reconciler.received_chunks))
                    state.expected_chunks = inferred
                    state.reconciler.expected_chunks = inferred
                await set_connection_state("done", "Feeder finished and closed socket.")
            else:
                await set_connection_state("disconnected", "Feeder disconnected before sending chunks.")
                
    except asyncio.CancelledError:
        pass
    except (ConnectionResetError, BrokenPipeError, ConnectionError) as e:
        await set_connection_state("dropped", f"Socket connection dropped unexpectedly: {e}")
    except Exception as e:
        await set_connection_state("dropped", f"Error during chunk reception: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        state.active_feeder_ip = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data exists on startup
    if not os.path.exists("data/chunks_feed.json") or not os.path.exists("data/ground_truth.json"):
        print("[Startup] Dataset not found, generating...")
        from generator import generate_and_save_dataset
        generate_and_save_dataset()

    # Start Async TCP Server with explicit 256KB line buffer limit to guard against truncation
    TCP_LINE_LIMIT = 256 * 1024
    print(f"[Startup] Starting TCP Server on {state.tcp_host}:{state.tcp_port} (limit={TCP_LINE_LIMIT} bytes)...")
    server = await asyncio.start_server(
        handle_tcp_client,
        state.tcp_host,
        state.tcp_port,
        limit=TCP_LINE_LIMIT
    )
    state.tcp_server = server
    print(f"[Startup] TCP Server listening on {state.tcp_host}:{state.tcp_port}")
    
    yield
    
    # Teardown
    print("[Shutdown] Shutting down TCP Server...")
    if state.tcp_server:
        state.tcp_server.close()
        await state.tcp_server.wait_closed()
        
    if state.feeder_subprocess and state.feeder_subprocess.poll() is None:
        print("[Shutdown] Terminating running feeder subprocess...")
        state.feeder_subprocess.terminate()


app = FastAPI(title="Transcript Timing Reconciler", lifespan=lifespan)

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.websocket_clients.add(websocket)
    try:
        # Send initial snapshot
        full_transcript = state.reconciler.build_full_transcript()
        await websocket.send_text(json.dumps({
            "event": "init",
            "seq": state.ws_sequence,
            "data": {
                "connection_state": state.connection_state,
                "message": state.last_event_message,
                "stats": state.reconciler.get_stats(expected_chunks=state.expected_chunks),
                "transcript": [w.to_dict() for w in full_transcript],
                "resolved_seams": [s.to_dict() for s in state.reconciler.resolved_seams.values()],
                "feeder_logs": state.feeder_logs[-20:]
            }
        }))
        while True:
            # Keep socket open and handle incoming client commands if any
            data = await websocket.receive_text()
            try:
                cmd = json.loads(data)
                if cmd.get("action") == "resync":
                    full_transcript = state.reconciler.build_full_transcript()
                    await websocket.send_text(json.dumps({
                        "event": "resync",
                        "seq": state.ws_sequence,
                        "data": {
                            "stats": state.reconciler.get_stats(expected_chunks=state.expected_chunks),
                            "transcript": [w.to_dict() for w in full_transcript],
                            "resolved_seams": [s.to_dict() for s in state.reconciler.resolved_seams.values()],
                            "feeder_logs": state.feeder_logs[-20:]
                        }
                    }))
            except Exception:
                pass
    except WebSocketDisconnect:
        state.websocket_clients.discard(websocket)
    except Exception:
        state.websocket_clients.discard(websocket)


@app.get("/api/status")
async def get_status():
    return {
        "connection_state": state.connection_state,
        "message": state.last_event_message,
        "active_feeder": state.active_feeder_ip,
        "tcp_port": state.tcp_port,
        "feeder_running": (state.feeder_subprocess is not None and state.feeder_subprocess.poll() is None),
        "feeder_logs": state.feeder_logs[-20:],
        "stats": state.reconciler.get_stats(expected_chunks=state.expected_chunks)
    }


@app.get("/api/transcript")
async def get_transcript():
    words = state.reconciler.build_full_transcript()
    return {
        "word_count": len(words),
        "words": [w.to_dict() for w in words],
        "seams": [s.to_dict() for s in state.reconciler.resolved_seams.values()]
    }


class ConfigModel(BaseModel):
    bias: str


@app.post("/api/config")
async def update_config(config: ConfigModel):
    if config.bias not in ("anti-duplication", "anti-drop"):
        raise HTTPException(status_code=400, detail="Invalid bias. Choose 'anti-duplication' or 'anti-drop'.")
    state.reconciler.bias = config.bias
    # Re-evaluate all seams if chunks are already present
    if state.reconciler.received_chunks:
        # Offload CPU-intensive sequence alignment re-resolution to thread pool executor
        loop = asyncio.get_running_loop()

        def re_resolve_all_seams():
            state.reconciler.resolved_seams.clear()
            for cid in sorted(state.reconciler.received_chunks.keys()):
                if (cid - 1) in state.reconciler.received_chunks:
                    state.reconciler._resolve_seam(cid - 1, cid)
            return state.reconciler.build_full_transcript()

        full_transcript = await loop.run_in_executor(None, re_resolve_all_seams)

        state.broadcast_word_keys.clear()
        for w in full_transcript:
            if w.word_index is not None:
                state.broadcast_word_keys.add((w.chunk_id, w.word_index))
            if w.alt_chunk_id is not None and w.alt_word_index is not None:
                state.broadcast_word_keys.add((w.alt_chunk_id, w.alt_word_index))
            state.broadcast_word_keys.add((w.chunk_id, w.text, round(w.start, 2)))
        await broadcast_ws("transcript_update", {
            "stats": state.reconciler.get_stats(expected_chunks=state.expected_chunks),
            "transcript": [w.to_dict() for w in full_transcript],
            "resolved_seams": [s.to_dict() for s in state.reconciler.resolved_seams.values()]
        })
    return {"status": "ok", "bias": state.reconciler.bias}


@app.post("/api/reset")
async def reset_reconciler():
    state.reconciler.reset()
    state.connection_state = "disconnected"
    state.last_event_message = "Reconciler reset. Ready for feeder."
    state.expected_chunks = None
    state.highest_chunk_id = -1
    state.total_packets_sent = None
    state.broadcast_word_keys.clear()
    state.feeder_logs.clear()
    await broadcast_ws("reset", {
        "stats": state.reconciler.get_stats(expected_chunks=None),
        "transcript": []
    })
    return {"status": "ok", "message": "Reconciler reset successfully."}


class FeederLaunchModel(BaseModel):
    delay: float = 0.04


@app.post("/api/feeder/start")
async def start_feeder_process(cfg: FeederLaunchModel):
    """
    Spawns feeder.py as a real, distinct OS process over the real TCP socket.
    Asynchronously drains stdout/stderr in background threads to avoid pipe buffer deadlocks,
    and streams feeder logs to WebSocket clients.
    """
    if state.feeder_subprocess and state.feeder_subprocess.poll() is None:
        return {"status": "already_running", "pid": state.feeder_subprocess.pid}
        
    cmd = [
        sys.executable,
        "feeder.py",
        "--host", state.tcp_host,
        "--port", str(state.tcp_port),
        "--delay", str(cfg.delay)
    ]
    
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    state.feeder_subprocess = p
    state.feeder_pid = p.pid

    loop = asyncio.get_running_loop()

    def drain_stream(stream, prefix=""):
        try:
            for line in iter(stream.readline, ''):
                clean_line = line.strip()
                if clean_line:
                    log_entry = f"{prefix}{clean_line}" if prefix else clean_line
                    state.add_feeder_log(log_entry)
                    asyncio.run_coroutine_threadsafe(
                        broadcast_ws("feeder_log", {"log": log_entry}),
                        loop
                    )
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threading.Thread(target=drain_stream, args=(p.stdout,), daemon=True).start()
    threading.Thread(target=drain_stream, args=(p.stderr, "[STDERR] "), daemon=True).start()

    print(f"[API] Spawned feeder process with PID {p.pid}")
    return {"status": "started", "pid": p.pid, "delay": cfg.delay}


@app.post("/api/feeder/stop")
async def stop_feeder_process():
    if state.feeder_subprocess and state.feeder_subprocess.poll() is None:
        state.feeder_subprocess.terminate()
        return {"status": "terminated"}
    return {"status": "not_running"}
