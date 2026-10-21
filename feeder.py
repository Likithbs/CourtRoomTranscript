"""
Feeder Process.
Reads transcription chunk packets from disk and streams them across a real TCP socket
to the Reconciler Service process, one message per chunk.
"""

import socket
import json
import time
import argparse
import sys
import os

def run_feeder(
    host: str = "127.0.0.1",
    port: int = 9000,
    feed_file: str = "data/chunks_feed.json",
    delay: float = 0.05,
    max_retries: int = 15,
    retry_interval: float = 1.0
):
    if not os.path.exists(feed_file):
        print(f"[Feeder] Error: Feed file '{feed_file}' not found.")
        print(f"[Feeder] Generating dataset first...")
        from generator import generate_and_save_dataset
        generate_and_save_dataset()

    with open(feed_file, "r", encoding="utf-8") as f:
        packets = json.load(f)
        
    total_packets = len(packets)
    print(f"[Feeder] Loaded {total_packets} packets from '{feed_file}'.")
    print(f"[Feeder] Attempting to connect to Reconciler at {host}:{port}...")
    
    sock = None
    for attempt in range(1, max_retries + 1):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            print(f"[Feeder] Connected to {host}:{port} successfully on attempt {attempt}.")
            break
        except ConnectionRefusedError:
            print(f"[Feeder] Attempt {attempt}/{max_retries}: Connection refused. Reconciler not ready yet, retrying in {retry_interval}s...")
            time.sleep(retry_interval)
            sock = None
            
    if not sock:
        print(f"[Feeder] FATAL: Could not connect to Reconciler service at {host}:{port} after {max_retries} attempts.")
        sys.exit(1)
        
    try:
        print(f"[Feeder] Starting chunk transmission (inter-chunk delay: {delay}s)...")
        start_time = time.time()
        
        # Send stream manifest header before chunks
        unique_chunk_ids = {p.get("chunk_id") for p in packets if p.get("chunk_id") is not None}
        expected_chunks = len(unique_chunk_ids)
        manifest_packet = {
            "type": "stream_manifest",
            "total_packets": total_packets,
            "expected_chunks": expected_chunks
        }
        sock.sendall((json.dumps(manifest_packet) + "\n").encode("utf-8"))
        print(f"[Feeder] Sent stream_manifest (expected_chunks={expected_chunks}, total_packets={total_packets})")

        for idx, packet in enumerate(packets):
            # Serialize to NDJSON
            payload = json.dumps(packet) + "\n"
            sock.sendall(payload.encode("utf-8"))
            
            cid = packet.get("chunk_id")
            is_fail = packet.get("is_failure", False)
            status_label = "FAILED (to be retried)" if is_fail else f"SUCCESS ({len(packet.get('words', []))} words)"
            
            print(f"[Feeder] Sent packet {idx + 1}/{total_packets} -> Chunk {cid} [{status_label}]")
            
            if delay > 0:
                time.sleep(delay)
                
        # Send end-of-stream control message
        eos_packet = {"type": "stream_end", "total_sent": total_packets}
        sock.sendall((json.dumps(eos_packet) + "\n").encode("utf-8"))
        
        elapsed = time.time() - start_time
        print(f"[Feeder] Successfully streamed all {total_packets} packets in {elapsed:.2f}s.")
        print(f"[Feeder] Closing socket.")
    except (socket.error, BrokenPipeError, ConnectionResetError) as e:
        print(f"[Feeder] Socket connection dropped: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        print(f"[Feeder] Process exited.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Feed transcription chunks over a TCP socket")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Target TCP host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000, help="Target TCP port (default 9000)")
    parser.add_argument("--file", type=str, default="data/chunks_feed.json", help="Path to feed json file")
    parser.add_argument("--delay", type=float, default=0.04, help="Delay in seconds between packets (default 0.04s)")
    args = parser.parse_args()
    
    run_feeder(host=args.host, port=args.port, feed_file=args.file, delay=args.delay)
