"""
Verification script for the chunk generator.
Eyeballs overlap regions and confirms that timestamps disagree across overlapping chunks,
that chunks are out of order, that failures exist, and that duplicates exist.
"""

import json
import os
import sys

def verify_dataset():
    feed_path = os.path.join("data", "chunks_feed.json")
    gt_path = os.path.join("data", "ground_truth.json")
    
    if not os.path.exists(feed_path) or not os.path.exists(gt_path):
        print(f"Dataset not found! Running generator first...")
        from generator import generate_and_save_dataset
        generate_and_save_dataset()

    with open(feed_path, "r", encoding="utf-8") as f:
        packets = json.load(f)
        
    with open(gt_path, "r", encoding="utf-8") as f:
        ground_truth = json.load(f)
        
    print(f"\n================ DATASET VERIFICATION ================")
    print(f"Total Ground Truth Words: {len(ground_truth)}")
    print(f"Total Packets to be transmitted: {len(packets)}")
    
    # Check packet emission order
    first_10_chunk_ids = [p["chunk_id"] for p in packets[:10]]
    print(f"First 10 packet chunk IDs received: {first_10_chunk_ids}")
    is_shuffled = first_10_chunk_ids != sorted(first_10_chunk_ids)
    print(f"Are packets arriving out-of-order? {'YES (random order verified)' if is_shuffled else 'NO'}")
    
    # Check failures
    failure_packets = [p for p in packets if p.get("is_failure")]
    print(f"Number of failure packets: {len(failure_packets)} ({len(failure_packets)/len(packets)*100:.1f}%)")
    
    # Check duplicates
    chunk_counts = {}
    for p in packets:
        if not p.get("is_failure"):
            cid = p["chunk_id"]
            chunk_counts[cid] = chunk_counts.get(cid, 0) + 1
    duplicates = {cid: cnt for cid, cnt in chunk_counts.items() if cnt > 1}
    print(f"Number of chunks with duplicate successful arrivals: {len(duplicates)}")
    
    # Now EYEBALL the overlap regions between chunk 0 and chunk 1 (or other adjacent pairs)
    # Collect successful packets by chunk_id
    successful_by_id = {}
    for p in packets:
        if not p.get("is_failure") and p["chunk_id"] not in successful_by_id:
            successful_by_id[p["chunk_id"]] = p
            
    print(f"\n================ EYEBALLING OVERLAP SEAMS ================")
    # Check seam between chunk 0 (0-30s) and chunk 1 (25-55s)
    if 0 in successful_by_id and 1 in successful_by_id:
        c0 = successful_by_id[0]
        c1 = successful_by_id[1]
        
        # Words in c0 in overlap window [24.9, 30.1]
        overlap_c0 = [w for w in c0["words"] if w["start"] >= 24.5]
        # Words in c1 in overlap window [24.9, 30.1]
        overlap_c1 = [w for w in c1["words"] if w["start"] <= 30.5]
        
        print(f"\nComparing Overlap Seam: Chunk 0 [0-30s] vs Chunk 1 [25-55s]")
        print(f"Chunk 0 overlap words count: {len(overlap_c0)}")
        print(f"Chunk 1 overlap words count: {len(overlap_c1)}")
        print("-" * 75)
        print(f"{'Word Text':<20} | {'Chunk 0 Times':<22} | {'Chunk 1 Times':<22} | {'Start Diff (ms)':<15}")
        print("-" * 75)
        
        # Match words by word_id
        c1_dict = {w["word_id"]: w for w in overlap_c1}
        diffs = []
        for w0 in overlap_c0:
            wid = w0["word_id"]
            if wid in c1_dict:
                w1 = c1_dict[wid]
                t_diff_ms = abs(w0["start"] - w1["start"]) * 1000.0
                diffs.append(t_diff_ms)
                print(f"{w0['text']:<20} | {w0['start']:>6.3f}s - {w0['end']:>6.3f}s   | {w1['start']:>6.3f}s - {w1['end']:>6.3f}s   | {t_diff_ms:>7.1f} ms")
            else:
                print(f"{w0['text']:<20} | {w0['start']:>6.3f}s - {w0['end']:>6.3f}s   | {'[DROPPED IN C1]':<22} | N/A")
                
        # Check words in c1 not in c0
        c0_dict = {w["word_id"]: w for w in overlap_c0}
        for w1 in overlap_c1:
            if w1["word_id"] not in c0_dict:
                print(f"{w1['text']:<20} | {'[DROPPED IN C0]':<22} | {w1['start']:>6.3f}s - {w1['end']:>6.3f}s   | N/A")
                
        print("-" * 75)
        if diffs:
            avg_diff = sum(diffs) / len(diffs)
            max_diff = max(diffs)
            print(f"Timestamp disagreements verified! Mean diff: {avg_diff:.1f}ms, Max diff: {max_diff:.1f}ms")
            assert max_diff > 0, "Timestamps did not jitter!"
            print(f"PASSED: Timestamps genuinely disagree across overlapping chunks as specified!")
        else:
            print("WARNING: No matched words found in overlap.")
            
    print(f"==========================================================\n")

if __name__ == "__main__":
    verify_dataset()
