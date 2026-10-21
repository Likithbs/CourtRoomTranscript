"""
Ground Truth and Out-of-Order Messy Chunk Generator
Generates a 40-minute (2400-second) courtroom transcript ground truth
and slices it into overlapping, jittered, out-of-order, partially-failed chunks.
"""

import json
import random
import os
from typing import List, Dict, Any, Tuple

# Authentic courtroom vocabulary and dialogue segments for realistic simulation
COURT_SPEAKERS = ["THE COURT", "MR. VANCE (Prosecutor)", "MS. HOLLOWAY (Defense)", "THE WITNESS (Dr. Mercer)"]

SAMPLE_COURT_LINES = [
    ("THE COURT", "Please be seated. We are on the record in State versus Sterling, case number CR-2024-8842."),
    ("THE COURT", "Counsel, are the parties prepared to continue with the testimony of the state's forensic expert?"),
    ("MR. VANCE (Prosecutor)", "Yes, Your Honor. The People recall Dr. Aris Mercer to the stand."),
    ("THE COURT", "Dr. Mercer, you remain under oath. You may proceed, Mr. Vance."),
    ("MR. VANCE (Prosecutor)", "Thank you, Your Honor. Dr. Mercer, turning your attention back to the acoustic analysis performed on Exhibit 14."),
    ("THE WITNESS (Dr. Mercer)", "Certainly. Our laboratory processed the multi-channel digital audio recording recovered from the lobby surveillance device."),
    ("MR. VANCE (Prosecutor)", "Can you describe for the jury the methodology used to isolate the primary vocal frequencies?"),
    ("THE WITNESS (Dr. Mercer)", "We applied a finite impulse response bandpass filter between 300 hertz and 3400 hertz to eliminate HVAC rumble."),
    ("THE WITNESS (Dr. Mercer)", "Following that filtering, we conducted spectral subtraction to attenuate stationary background noise by approximately eighteen decibels."),
    ("MS. HOLLOWAY (Defense)", "Objection, Your Honor. Lack of foundation as to the calibration parameters of the software suite used."),
    ("THE COURT", "Overruled, Ms. Holloway. Dr. Mercer may explain the calibration standard before giving his conclusion."),
    ("THE WITNESS (Dr. Mercer)", "Thank you, Your Honor. The spectral calibration followed NIST Special Publication 500-293 guidelines for forensic audio integrity."),
    ("MR. VANCE (Prosecutor)", "And Dr. Mercer, upon completing that calibration, what did your temporal cross-correlation reveal regarding the two gunshots?"),
    ("THE WITNESS (Dr. Mercer)", "The cross-correlation showed an acoustic impulse interval of exactly 1.42 seconds between the first discharge and the second discharge."),
    ("MR. VANCE (Prosecutor)", "Was there any indication of audio tampering, splicing, or artificial time-dilation in that window?"),
    ("THE WITNESS (Dr. Mercer)", "None whatsoever. The phase continuity across channel A and channel B was completely unbroken throughout the forty-second interval."),
    ("MS. HOLLOWAY (Defense)", "Your Honor, may I approach the witness with defense exhibit G for identification?"),
    ("THE COURT", "You may approach."),
    ("MS. HOLLOWAY (Defense)", "Dr. Mercer, isn't it true that reverberant reflections in that tiled hallway could introduce spurious peaks in your correlation metric?"),
    ("THE WITNESS (Dr. Mercer)", "While reverberation does create multipath reflections, the direct path arrival always precedes the early reflections by at least twelve milliseconds."),
    ("MS. HOLLOWAY (Defense)", "And did you account for the acoustic impedance of the heavy glass partition situated between the microphone and the vestibule?"),
    ("THE WITNESS (Dr. Mercer)", "Yes, we modeled the glass transmission loss at approximately twenty-four decibels at one kilohertz."),
    ("THE COURT", "Let us keep the questions focused on the witness's direct observations, counsel."),
    ("MS. HOLLOWAY (Defense)", "Understood, Your Honor. Dr. Mercer, look at page nine of your supplemental report. Is that your signature at the bottom?"),
    ("THE WITNESS (Dr. Mercer)", "Yes, that is my signature and digital validation hash."),
    ("MS. HOLLOWAY (Defense)", "And in that report, you initially stated that the ambient noise level was too elevated to establish absolute voice certainty?"),
    ("THE WITNESS (Dr. Mercer)", "That initial note referred only to the unenhanced preliminary tape provided by the precinct, before high-resolution masters arrived."),
    ("MR. VANCE (Prosecutor)", "Redirect, Your Honor."),
    ("THE COURT", "Proceed on redirect."),
    ("MR. VANCE (Prosecutor)", "Dr. Mercer, to a reasonable degree of scientific certainty in your field, are you confident in the chronological sequence of events?"),
    ("THE WITNESS (Dr. Mercer)", "Yes, beyond any reasonable acoustic doubt. The timeline established by our analysis is robust, verified, and mathematically conclusive.")
]


def generate_ground_truth(total_duration: float = 2400.0, seed: int = 42) -> List[Dict[str, Any]]:
    """
    Generates a realistic stream of words with start and end timestamps
    spanning `total_duration` seconds (default 2400s = 40 minutes).
    Average speech rate: ~2.5 - 3.0 words per second.
    """
    rng = random.Random(seed)
    words: List[Dict[str, Any]] = []
    
    current_time = 0.5  # start after a brief 0.5s pause
    word_id = 0
    dialogue_idx = 0
    num_dialogues = len(SAMPLE_COURT_LINES)
    
    while current_time < total_duration - 1.0:
        speaker, line = SAMPLE_COURT_LINES[dialogue_idx % num_dialogues]
        dialogue_idx += 1
        
        # Add speaker tag occasionally as spoken words
        text_tokens = line.split()
        
        for token in text_tokens:
            if current_time >= total_duration - 0.5:
                break
            
            # Realistic word duration based on token length: 0.15s - 0.45s
            token_len = len(token)
            base_duration = 0.12 + (token_len * 0.035) + rng.uniform(-0.03, 0.05)
            duration = max(0.12, min(0.65, base_duration))
            
            start_time = round(current_time, 3)
            end_time = round(current_time + duration, 3)
            
            words.append({
                "word_id": word_id,
                "text": token,
                "start": start_time,
                "end": end_time
            })
            word_id += 1
            
            # Pause between words: short pause (0.04 - 0.12s), occasionally longer for punctuation
            pause = rng.uniform(0.04, 0.10)
            if token.endswith((".", "?", "!", ":", ";", ",")):
                pause += rng.uniform(0.15, 0.35)
            
            current_time = end_time + pause
            
        # Inter-turn speaker pause: 0.3s - 0.8s
        current_time += rng.uniform(0.3, 0.8)
        
    return words


def slice_into_overlapping_chunks(
    ground_truth_words: List[Dict[str, Any]],
    total_duration: float = 2400.0,
    chunk_duration: float = 30.0,
    overlap_duration: float = 5.0,
    jitter_max_seconds: float = 0.100,  # up to 100ms jitter
    failure_rate: float = 1.0 / 12.0,   # roughly 1 in 12 fails
    seed: int = 1337
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Slices the ground truth into 30-second chunks overlapping by 5 seconds (step = 25s).
    Applies timestamp jitter, edge dropped words risk, failures, re-sends, duplicates,
    and random shuffle.
    
    Returns:
        (clean_chunk_metadata, transmission_packets)
    """
    rng = random.Random(seed)
    step = chunk_duration - overlap_duration  # 25.0 seconds
    
    # Calculate chunk windows
    chunks_meta = []
    chunk_id = 0
    start = 0.0
    while start < total_duration:
        end = min(total_duration, start + chunk_duration)
        chunks_meta.append({
            "chunk_id": chunk_id,
            "window_start": round(start, 3),
            "window_end": round(end, 3)
        })
        if end >= total_duration:
            break
        start += step
        chunk_id += 1
        
    num_chunks = len(chunks_meta)
    
    # Build raw word lists for each chunk from ground truth
    raw_chunks: Dict[int, List[Dict[str, Any]]] = {}
    for meta in chunks_meta:
        cid = meta["chunk_id"]
        w_start = meta["window_start"]
        w_end = meta["window_end"]
        
        chunk_words = []
        for w in ground_truth_words:
            # Word belongs to this chunk if its ground-truth span intersects or falls inside window
            # Specifically, center of word or overlap with window
            word_mid = (w["start"] + w["end"]) / 2.0
            if w_start <= word_mid <= w_end:
                # Apply edge jitter risk:
                # If word is within 0.08s of window boundary, jitter may cause it to drop out of this chunk
                edge_dist = min(abs(word_mid - w_start), abs(word_mid - w_end))
                if edge_dist < 0.08 and rng.random() < 0.25:
                    # Edge dropout! Skip this word in this chunk
                    continue
                
                # Apply timestamp jitter independently for this chunk (up to +/- 100ms)
                j_start = rng.uniform(-jitter_max_seconds, jitter_max_seconds)
                j_end = rng.uniform(-jitter_max_seconds, jitter_max_seconds)
                
                # Jittered times must remain causal (start < end)
                clamped_start = max(w_start, round(w["start"] + j_start, 3))
                clamped_end = max(clamped_start + 0.05, round(w["end"] + j_end, 3))
                
                chunk_words.append({
                    "word_id": w["word_id"],  # ground truth id preserved for verification metrics
                    "text": w["text"],
                    "start": clamped_start,
                    "end": clamped_end
                })
        raw_chunks[cid] = chunk_words

    # Now create transmission packets:
    # 1. Successful chunks
    # 2. Failure packets (~1 in 12) sent before their eventual retry
    # 3. Duplicate transmissions (some chunks sent twice successfully)
    # 4. Out-of-order random emission
    
    packets = []
    
    # Select roughly 1 in 12 chunks to fail first before succeeding
    num_failures = max(1, round(num_chunks / 12.0))
    failed_chunk_ids = set(rng.sample(range(num_chunks), num_failures))
    
    # Select ~10% chunks to arrive as duplicates
    num_duplicates = max(1, round(num_chunks * 0.10))
    duplicate_chunk_ids = set(rng.sample(range(num_chunks), num_duplicates))

    for meta in chunks_meta:
        cid = meta["chunk_id"]
        w_start = meta["window_start"]
        w_end = meta["window_end"]
        words = raw_chunks[cid]
        
        # If this chunk fails, emit failure packet
        if cid in failed_chunk_ids:
            packets.append({
                "type": "chunk",
                "chunk_id": cid,
                "window_start": w_start,
                "window_end": w_end,
                "is_failure": True,
                "error_message": f"ASR worker execution failed on chunk {cid}: timeout",
                "words": []
            })
            
        # Successful packet
        packets.append({
            "type": "chunk",
            "chunk_id": cid,
            "window_start": w_start,
            "window_end": w_end,
            "is_failure": False,
            "error_message": None,
            "words": words
        })
        
        # Duplicate successful arrival
        if cid in duplicate_chunk_ids:
            packets.append({
                "type": "chunk",
                "chunk_id": cid,
                "window_start": w_start,
                "window_end": w_end,
                "is_failure": False,
                "error_message": None,
                "words": words
            })

    # Shuffle packets so they arrive in random order
    rng.shuffle(packets)
    
    return chunks_meta, packets


def generate_and_save_dataset(
    output_dir: str = "data",
    total_duration: float = 2400.0,
    seed: int = 2024
) -> Tuple[str, str]:
    """
    Generates the complete 40-minute dataset and saves:
      1. ground_truth.json
      2. chunks_feed.json
    """
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"[Generator] Generating {total_duration}s (40-minute) ground truth transcript...")
    ground_truth = generate_ground_truth(total_duration=total_duration, seed=seed)
    print(f"[Generator] Generated {len(ground_truth)} ground truth words.")
    
    print(f"[Generator] Cutting into 30s chunks (5s overlap), applying jitter, failures, and duplicates...")
    chunks_meta, packets = slice_into_overlapping_chunks(
        ground_truth,
        total_duration=total_duration,
        seed=seed
    )
    
    print(f"[Generator] Created {len(chunks_meta)} unique chunks (~{len(chunks_meta)-1} seams).")
    print(f"[Generator] Created {len(packets)} transmission packets (with failures and retries).")
    
    gt_file = os.path.join(output_dir, "ground_truth.json")
    with open(gt_file, "w", encoding="utf-8") as f:
        json.dump(ground_truth, f, indent=2)
        
    feed_file = os.path.join(output_dir, "chunks_feed.json")
    with open(feed_file, "w", encoding="utf-8") as f:
        json.dump(packets, f, indent=2)
        
    print(f"[Generator] Successfully saved to '{gt_file}' and '{feed_file}'.")
    return gt_file, feed_file


if __name__ == "__main__":
    generate_and_save_dataset()
