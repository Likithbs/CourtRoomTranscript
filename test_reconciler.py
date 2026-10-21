"""
Unit and Integration Tests for Transcript Reconciler.
"""

import json
import pytest
from reconciler import TranscriptReconciler, Word, align_overlap_words, normalize_token

def test_normalize_token():
    assert normalize_token("Hello,") == "hello"
    assert normalize_token("\"State\"") == "state"
    assert normalize_token("honor.") == "honor"
    assert normalize_token("witness?") == "witness"


def test_cross_utterance_tiebreaker():
    """Verify that identical tokens from distant utterances (far word_ids) are not spuriously aligned."""
    words_left = [
        Word("Your", 28.0, 28.3, chunk_id=0, ground_truth_id=10),
        Word("Honor", 28.4, 28.8, chunk_id=0, ground_truth_id=11),
    ]
    words_right = [
        Word("Your", 28.05, 28.35, chunk_id=1, ground_truth_id=150),
        Word("Honor", 28.45, 28.85, chunk_id=1, ground_truth_id=151),
    ]
    seam = align_overlap_words(words_left, words_right, bias="anti-duplication")
    assert seam.aligned_count == 0


def test_align_overlap_words_exact():
    # Simulate overlapping words with ~80ms jitter
    words_left = [
        Word("Dr.", 27.949, 28.188, chunk_id=0),
        Word("Mercer,", 28.338, 28.725, chunk_id=0),
        Word("turning", 29.116, 29.683, chunk_id=0),
        Word("your", 29.659, 30.023, chunk_id=0),
    ]
    words_right = [
        Word("Dr.", 27.874, 28.084, chunk_id=1),
        Word("Mercer,", 28.344, 28.737, chunk_id=1),
        Word("turning", 29.158, 29.641, chunk_id=1),
        Word("your", 29.674, 29.991, chunk_id=1),
    ]
    
    seam = align_overlap_words(words_left, words_right, bias="anti-duplication")
    assert seam.aligned_count == 4
    assert seam.suppressed_count == 0
    assert len(seam.words) == 4
    # Check that reconciled start times are between the two jittered times
    for w in seam.words:
        assert w.is_seam_resolved is True


def test_anti_duplication_vs_anti_drop_on_edge_words():
    # Left chunk has an extra echo/duplicate word near seam
    words_left = [
        Word("witness", 28.0, 28.3, chunk_id=0),
        Word("testified", 28.5, 28.8, chunk_id=0),
    ]
    words_right = [
        Word("testified", 28.52, 28.82, chunk_id=1),
        Word("truthfully", 29.0, 29.4, chunk_id=1),
    ]
    
    seam_dedup = align_overlap_words(words_left, words_right, bias="anti-duplication")
    seam_drop = align_overlap_words(words_left, words_right, bias="anti-drop")
    
    # Both should align 'testified'
    assert seam_dedup.aligned_count == 1
    assert seam_drop.aligned_count == 1
    
    # Check word texts in order
    dedup_texts = [w.text for w in seam_dedup.words]
    drop_texts = [w.text for w in seam_drop.words]
    
    assert "witness" in dedup_texts
    assert "testified" in dedup_texts
    assert "truthfully" in dedup_texts
    assert dedup_texts == ["witness", "testified", "truthfully"]


def test_out_of_order_and_duplicates():
    reconciler = TranscriptReconciler(bias="anti-duplication")
    
    # Chunk 1 arrives FIRST
    p1 = {
        "chunk_id": 1,
        "window_start": 25.0,
        "window_end": 55.0,
        "is_failure": False,
        "words": [
            {"text": "turning", "start": 29.158, "end": 29.641},
            {"text": "your", "start": 29.674, "end": 29.991},
            {"text": "attention", "start": 30.052, "end": 30.372},
        ]
    }
    res1 = reconciler.ingest_packet(p1)
    assert res1["status"] == "chunk_ingested"
    assert len(reconciler.resolved_seams) == 0  # No neighbors yet
    
    # Chunk 0 arrives SECOND (out-of-order)
    p0 = {
        "chunk_id": 0,
        "window_start": 0.0,
        "window_end": 30.0,
        "is_failure": False,
        "words": [
            {"text": "Proceed", "start": 23.5, "end": 24.0},
            {"text": "turning", "start": 29.116, "end": 29.683},
            {"text": "your", "start": 29.659, "end": 30.023},
        ]
    }
    res0 = reconciler.ingest_packet(p0)
    assert res0["status"] == "chunk_ingested"
    assert len(reconciler.resolved_seams) == 1  # Seam (0, 1) resolved!
    assert (0, 1) in reconciler.resolved_seams
    
    # Chunk 0 arrives THIRD (duplicate)
    res_dup = reconciler.ingest_packet(p0)
    assert res_dup["status"] == "duplicate_ignored"
    assert reconciler.duplicate_chunks_ignored == 1
    
    # Chunk 2 arrives as FAILURE
    p2_fail = {
        "chunk_id": 2,
        "window_start": 50.0,
        "window_end": 80.0,
        "is_failure": True,
        "words": []
    }
    res_fail = reconciler.ingest_packet(p2_fail)
    assert res_fail["status"] == "failure_logged"
    assert reconciler.failed_chunks_received == 1
    
    # Check assembled transcript so far
    transcript = reconciler.build_full_transcript()
    words = [w.text for w in transcript]
    print(f"Assembled words: {words}")
    assert words == ["Proceed", "turning", "your", "attention"]
    # Check that 'turning' and 'your' appear EXACTLY ONCE (no duplication!)
    assert words.count("turning") == 1
    assert words.count("your") == 1


def test_full_dataset_reconciliation():
    """Test full feed of 114 packets across 40 minutes."""
    with open("data/chunks_feed.json", "r", encoding="utf-8") as f:
        packets = json.load(f)
    with open("data/ground_truth.json", "r", encoding="utf-8") as f:
        ground_truth = json.load(f)
        
    reconciler = TranscriptReconciler(bias="anti-duplication")
    for p in packets:
        reconciler.ingest_packet(p)
        
    stats = reconciler.get_stats(expected_chunks=96)
    print("\nFull Dataset Reconciliation Stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
        
    assert stats["chunks_successful"] == 96
    assert stats["seams_resolved"] == 95
    assert stats["duplicates_ignored"] >= 8
    assert stats["chunks_failed"] >= 6
    
    full_transcript = reconciler.build_full_transcript()
    print(f"Total ground truth words: {len(ground_truth)}")
    print(f"Total reconciled words: {len(full_transcript)}")
    
    # Word count should be very close to ground truth (within 2%)
    diff_count = abs(len(full_transcript) - len(ground_truth))
    pct_diff = diff_count / len(ground_truth)
    print(f"Word count difference: {diff_count} ({pct_diff*100:.2f}%)")
    assert pct_diff < 0.05, f"Word count diverged too much: {pct_diff*100:.2f}%"


def test_transcript_caching_and_dirty_flag():
    """Verify that build_full_transcript caches output and dirty-flags appropriately."""
    reconciler = TranscriptReconciler(bias="anti-duplication")
    assert reconciler._is_dirty is False
    assert reconciler.build_full_transcript() == []
    
    # Ingest chunk 0
    p0 = {
        "chunk_id": 0,
        "window_start": 0.0,
        "window_end": 30.0,
        "is_failure": False,
        "words": [{"text": "Hello", "start": 1.0, "end": 1.5}]
    }
    reconciler.ingest_packet(p0)
    assert reconciler._is_dirty is True
    
    # Build transcript
    t1 = reconciler.build_full_transcript()
    assert reconciler._is_dirty is False
    assert len(t1) == 1
    
    # Repeated calls should return the identical cached list
    t2 = reconciler.build_full_transcript()
    assert t1 is t2  # Exact same cached instance
    assert reconciler._is_dirty is False

    # Duplicate packet does not dirty cache
    reconciler.ingest_packet(p0)
    assert reconciler._is_dirty is False
    assert reconciler.build_full_transcript() is t1

    # Failure packet does not dirty cache
    reconciler.ingest_packet({
        "chunk_id": 99,
        "is_failure": True,
        "words": []
    })
    assert reconciler._is_dirty is False
    assert reconciler.build_full_transcript() is t1

    # New chunk dirties cache
    p1 = {
        "chunk_id": 1,
        "window_start": 25.0,
        "window_end": 55.0,
        "is_failure": False,
        "words": [{"text": "World", "start": 32.0, "end": 32.5}]
    }
    reconciler.ingest_packet(p1)
    assert reconciler._is_dirty is True
    
    t3 = reconciler.build_full_transcript()
    assert reconciler._is_dirty is False
    assert len(t3) == 2


def test_get_stats_does_not_trigger_rebuild():
    """Verify that get_stats reads from cache and does not rebuild when dirty."""
    reconciler = TranscriptReconciler(bias="anti-duplication")
    p0 = {
        "chunk_id": 0,
        "window_start": 0.0,
        "window_end": 30.0,
        "is_failure": False,
        "words": [{"text": "Hello", "start": 1.0, "end": 1.5}]
    }
    reconciler.ingest_packet(p0)
    reconciler.build_full_transcript()
    
    # Ingest chunk 1, marking dirty
    p1 = {
        "chunk_id": 1,
        "window_start": 25.0,
        "window_end": 55.0,
        "is_failure": False,
        "words": [{"text": "World", "start": 32.0, "end": 32.5}]
    }
    reconciler.ingest_packet(p1)
    assert reconciler._is_dirty is True
    
    # Calling get_stats must NOT clear _is_dirty and must report cached word count
    stats = reconciler.get_stats()
    assert reconciler._is_dirty is True
    assert stats["total_reconciled_words"] == 1  # From previous cache before rebuild


def test_expected_chunks_inference():
    """Verify that expected_chunks is properly inferred or configured dynamically."""
    reconciler = TranscriptReconciler()
    # No chunks received
    assert reconciler.get_stats()["expected_chunks"] == 0
    assert reconciler.get_stats()["chunks_complete_pct"] == 0.0
    
    # Ingest chunks 0, 1, 2
    for cid in [0, 1, 2]:
        reconciler.ingest_packet({
            "chunk_id": cid,
            "window_start": cid * 25.0,
            "window_end": cid * 25.0 + 30.0,
            "is_failure": False,
            "words": []
        })
        
    # Inferred from highest chunk_id (2 + 1 = 3)
    stats = reconciler.get_stats()
    assert stats["expected_chunks"] == 3
    assert stats["chunks_successful"] == 3
    assert stats["chunks_complete_pct"] == 100.0
    
    # Explicitly configured on reconciler
    reconciler.expected_chunks = 10
    stats2 = reconciler.get_stats()
    assert stats2["expected_chunks"] == 10
    assert stats2["chunks_successful"] == 3
    assert stats2["chunks_complete_pct"] == 30.0


def test_dynamic_margin_derivation():
    """Verify that margin is derived dynamically from chunk metadata and not hardcoded."""
    reconciler = TranscriptReconciler()
    
    # Standard 5.0s overlap (chunk 0: [0, 30], chunk 1: [25, 55])
    # overlap_duration = 30.0 - 25.0 = 5.0s -> margin = min(5.0, 0.5) = 0.5
    reconciler.ingest_packet({
        "chunk_id": 0,
        "window_start": 0.0,
        "window_end": 30.0,
        "is_failure": False,
        "words": [{"text": "First", "start": 1.0, "end": 1.5}]
    })
    reconciler.ingest_packet({
        "chunk_id": 1,
        "window_start": 25.0,
        "window_end": 55.0,
        "is_failure": False,
        "words": [{"text": "Second", "start": 35.0, "end": 35.5}]
    })
    
    seam_01 = reconciler.resolved_seams.get((0, 1))
    assert seam_01 is not None
    assert seam_01.margin == 0.5
    
    # Custom small 0.2s overlap (chunk 2: [54.8, 80], overlap with chunk 1 is 55.0 - 54.8 = 0.2s)
    # margin should be min(0.2, 0.5) = 0.2
    reconciler.ingest_packet({
        "chunk_id": 2,
        "window_start": 54.8,
        "window_end": 80.0,
        "is_failure": False,
        "words": [{"text": "Third", "start": 60.0, "end": 60.5}]
    })
    seam_12 = reconciler.resolved_seams.get((1, 2))
    assert seam_12 is not None
    assert round(seam_12.margin, 3) == 0.2


def test_bisect_cutoff_lookups():
    """Verify that bisect properly partitions words into prefix, middle, and suffix."""
    reconciler = TranscriptReconciler()
    # Chunk 0 with 5 words at various timestamps
    reconciler.ingest_packet({
        "chunk_id": 0,
        "window_start": 0.0,
        "window_end": 30.0,
        "is_failure": False,
        "words": [
            {"text": "w0_1", "start": 5.0, "end": 5.5},
            {"text": "w0_2", "start": 10.0, "end": 10.5},
            {"text": "w0_3", "start": 24.0, "end": 24.4},
            {"text": "match", "start": 27.0, "end": 27.5},
        ]
    })
    # Chunk 1
    reconciler.ingest_packet({
        "chunk_id": 1,
        "window_start": 25.0,
        "window_end": 55.0,
        "is_failure": False,
        "words": [
            {"text": "match", "start": 27.05, "end": 27.55},
            {"text": "w1_1", "start": 35.0, "end": 35.5},
            {"text": "w1_2", "start": 40.0, "end": 40.5},
        ]
    })
    
    full_transcript = reconciler.build_full_transcript()
    words = [w.text for w in full_transcript]
    assert words == ["w0_1", "w0_2", "w0_3", "match", "w1_1", "w1_2"]


