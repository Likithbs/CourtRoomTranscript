"""
One-Command Launcher for Transcript Timing Reconciler.

Starts:
1. Data generator (if ground_truth.json or chunks_feed.json is missing).
2. FastAPI Reconciler Service (HTTP on :8000, TCP Socket Server on :9000).
3. Optionally launches Feeder process as a separate OS process communicating over TCP.
"""

import sys
import os
import subprocess
import time
import argparse
import webbrowser

def main():
    parser = argparse.ArgumentParser(description="Transcript Timing Reconciler Launcher")
    parser.add_argument("--feed", action="store_true", help="Automatically launch feeder in a separate process on start")
    parser.add_argument("--feed-delay", type=float, default=0.04, help="Feeder packet delay in seconds (default 0.04)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port for browser UI (default 8000)")
    parser.add_argument("--tcp-port", type=int, default=9000, help="TCP port for feeder socket (default 9000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    args = parser.parse_args()

    # 1. Check / Generate Dataset
    gt_file = os.path.join("data", "ground_truth.json")
    feed_file = os.path.join("data", "chunks_feed.json")
    if not os.path.exists(gt_file) or not os.path.exists(feed_file):
        print("\n=======================================================")
        print("  STEP 1: Generating 40-minute synthetic transcript...")
        print("=======================================================")
        from generator import generate_and_save_dataset
        generate_and_save_dataset()
    else:
        print("[Launcher] Found existing 40-minute dataset in 'data/'.")

    # 2. Start FastAPI Server
    print("\n=======================================================")
    print("  STEP 2: Starting Reconciler Service...")
    print(f"  Web UI: http://localhost:{args.port}")
    print(f"  TCP Socket Server: 127.0.0.1:{args.tcp_port}")
    print("=======================================================\n")

    if not args.no_browser:
        # Open browser after short pause
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(f"http://localhost:{args.port}")
        import threading
        threading.Thread(target=open_browser, daemon=True).start()

    # 3. Optional auto-feed
    if args.feed:
        def launch_feeder():
            time.sleep(2.0)
            print(f"\n[Launcher] Launching Feeder as separate OS process...")
            subprocess.Popen([
                sys.executable, "feeder.py",
                "--port", str(args.tcp_port),
                "--delay", str(args.feed_delay)
            ])
        import threading
        threading.Thread(target=launch_feeder, daemon=True).start()

    # Launch uvicorn
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=args.port, log_level="info")

if __name__ == "__main__":
    main()
