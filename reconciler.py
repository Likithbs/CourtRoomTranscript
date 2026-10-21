"""
Core Reconciler Engine for Out-of-Order Overlapping Caption Chunks.

Handles:
- Out-of-order chunk arrivals
- Duplicate chunk arrivals (idempotence)
- ASR failure chunks (tracking & discarding empty payloads)
- Overlap seam resolution via Needleman-Wunsch alignment with time affinity
- Configurable tension resolution: 'anti-duplication' (default & defended) vs 'anti-drop'
- Real-time assembly of ordered continuous transcript
"""

import math
import bisect
from typing import List, Dict, Any, Optional, Tuple, Set


class Word:
    def __init__(
        self,
        text: str,
        start: float,
        end: float,
        chunk_id: int,
        ground_truth_id: Optional[int] = None,
        is_seam_resolved: bool = False,
        seam_id: Optional[str] = None,
        resolution_note: Optional[str] = None,
        word_index: Optional[int] = None,
        alt_chunk_id: Optional[int] = None,
        alt_word_index: Optional[int] = None
    ):
        self.text = text
        self.start = round(start, 3)
        self.end = round(end, 3)
        self.chunk_id = chunk_id
        self.ground_truth_id = ground_truth_id
        self.is_seam_resolved = is_seam_resolved
        self.seam_id = seam_id
        self.resolution_note = resolution_note
        self.word_index = word_index
        self.alt_chunk_id = alt_chunk_id
        self.alt_word_index = alt_word_index

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "chunk_id": self.chunk_id,
            "ground_truth_id": self.ground_truth_id,
            "is_seam_resolved": self.is_seam_resolved,
            "seam_id": self.seam_id,
            "resolution_note": self.resolution_note,
            "word_index": self.word_index
        }


def normalize_token(text: str) -> str:
    """Strip common punctuation and lowercase for phonetic/linguistic matching."""
    return text.lower().strip(".,!?:;\"'()[]{}")


class SeamResult:
    def __init__(
        self,
        seam_id: str,
        left_chunk_id: int,
        right_chunk_id: int,
        overlap_start: float,
        overlap_end: float,
        aligned_count: int,
        suppressed_count: int,
        retained_count: int,
        words: List[Word],
        details: List[str],
        margin: float = 0.5
    ):
        self.seam_id = seam_id
        self.left_chunk_id = left_chunk_id
        self.right_chunk_id = right_chunk_id
        self.overlap_start = overlap_start
        self.overlap_end = overlap_end
        self.aligned_count = aligned_count
        self.suppressed_count = suppressed_count
        self.retained_count = retained_count
        self.words = words
        self.details = details
        self.margin = margin

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seam_id": self.seam_id,
            "left_chunk_id": self.left_chunk_id,
            "right_chunk_id": self.right_chunk_id,
            "overlap_start": self.overlap_start,
            "overlap_end": self.overlap_end,
            "aligned_count": self.aligned_count,
            "suppressed_count": self.suppressed_count,
            "retained_count": self.retained_count,
            "margin": self.margin,
            "details": self.details
        }


def align_overlap_words(
    words_left: List[Word],
    words_right: List[Word],
    bias: str = "anti-duplication",
    overlap_start: float = 0.0,
    overlap_end: float = 0.0,
    margin: float = 0.5
) -> SeamResult:
    """
    Performs dynamic programming sequence alignment between words in the left chunk's overlap tail
    and the right chunk's overlap head.
    
    Tension Bias:
    - 'anti-duplication' (Defended Court Reporter standard):
      Suppresses unaligned words in the overlap if they closely shadow an aligned word in time,
      guaranteeing zero repeated words at boundaries.
    - 'anti-drop':
      Preserves all unaligned words from both chunks, eliminating dropped words at the cost
      of potential stutter/duplication.
    """
    n = len(words_left)
    m = len(words_right)
    
    # DP Matrix initialization: score, traceback
    # traceback: 0=diag (match/sub), 1=up (gap in right), 2=left (gap in left)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    tb = [[0] * (m + 1) for _ in range(n + 1)]
    
    gap_penalty = -1.5
    
    for i in range(1, n + 1):
        dp[i][0] = i * gap_penalty
        tb[i][0] = 1
    for j in range(1, m + 1):
        dp[0][j] = j * gap_penalty
        tb[0][j] = 2
        
    for i in range(1, n + 1):
        w_l = words_left[i - 1]
        norm_l = normalize_token(w_l.text)
        
        for j in range(1, m + 1):
            w_r = words_right[j - 1]
            norm_r = normalize_token(w_r.text)
            
            # Match score calculation
            if norm_l == norm_r:
                time_diff = abs(w_l.start - w_r.start)
                # Word match with close timestamp (< 0.40s) gets strong positive score
                if time_diff < 0.40:
                    match_score = 3.0 - (time_diff * 2.0)
                else:
                    # Same word token but spoken at completely different times
                    match_score = -2.0

                # Cross-utterance tiebreaker using ground truth word_id
                # Penalize lexically identical words from different utterances whose word_ids are far apart
                if w_l.ground_truth_id is not None and w_r.ground_truth_id is not None:
                    id_diff = abs(w_l.ground_truth_id - w_r.ground_truth_id)
                    if id_diff == 0:
                        match_score += 0.5
                    elif id_diff > 3:
                        match_score = -3.0
            else:
                match_score = -2.5
                
            diag = dp[i - 1][j - 1] + match_score
            up = dp[i - 1][j] + gap_penalty
            left = dp[i][j - 1] + gap_penalty
            
            best = diag
            best_dir = 0
            if up > best:
                best = up
                best_dir = 1
            if left > best:
                best = left
                best_dir = 2
                
            dp[i][j] = best
            tb[i][j] = best_dir
            
    # Traceback
    i = n
    j = m
    aligned_pairs: List[Tuple[Optional[Word], Optional[Word]]] = []
    
    while i > 0 or j > 0:
        if i > 0 and j > 0 and tb[i][j] == 0:
            aligned_pairs.append((words_left[i - 1], words_right[j - 1]))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or tb[i][j] == 1):
            aligned_pairs.append((words_left[i - 1], None))
            i -= 1
        else:
            aligned_pairs.append((None, words_right[j - 1]))
            j -= 1
            
    aligned_pairs.reverse()
    
    # Reconcile aligned pairs into final word list
    reconciled_words: List[Word] = []
    aligned_count = 0
    suppressed_count = 0
    retained_count = 0
    details = []
    
    c_left_id = words_left[0].chunk_id if words_left else -1
    c_right_id = words_right[0].chunk_id if words_right else -1
    seam_id = f"Seam[{c_left_id}->{c_right_id}]"
    
    # Pre-gather aligned times for shadow detection in anti-duplication mode
    matched_left_times = [pair[0].start for pair in aligned_pairs if pair[0] and pair[1] and normalize_token(pair[0].text) == normalize_token(pair[1].text)]
    
    for w_l, w_r in aligned_pairs:
        if w_l and w_r and normalize_token(w_l.text) == normalize_token(w_r.text):
            # Perfect matched pair!
            aligned_count += 1
            # Reconcile timestamp to midpoint to cancel out independent jitter
            reconciled_start = round((w_l.start + w_r.start) / 2.0, 3)
            reconciled_end = round((w_l.end + w_r.end) / 2.0, 3)
            # Retain ground truth id if available
            gt_id = w_l.ground_truth_id if w_l.ground_truth_id is not None else w_r.ground_truth_id
            
            reconciled_words.append(Word(
                text=w_l.text,
                start=reconciled_start,
                end=reconciled_end,
                chunk_id=w_l.chunk_id,
                ground_truth_id=gt_id,
                is_seam_resolved=True,
                seam_id=seam_id,
                resolution_note=f"Aligned '{w_l.text}' (dt={abs(w_l.start - w_r.start)*1000:.1f}ms)",
                word_index=w_l.word_index,
                alt_chunk_id=w_r.chunk_id,
                alt_word_index=w_r.word_index
            ))
            details.append(f"ALIGNED: '{w_l.text}' at {reconciled_start:.3f}s (C{w_l.chunk_id} & C{w_r.chunk_id})")
        elif w_l and not w_r:
            # Word only in left chunk
            if bias == "anti-duplication":
                # Check if this word temporally shadows any matched word
                shadowed = any(abs(w_l.start - mt) < 0.20 for mt in matched_left_times)
                if shadowed:
                    suppressed_count += 1
                    details.append(f"SUPPRESSED_LEFT: '{w_l.text}' at {w_l.start:.3f}s (shadowed duplicate)")
                    continue
            retained_count += 1
            reconciled_words.append(Word(
                text=w_l.text,
                start=w_l.start,
                end=w_l.end,
                chunk_id=w_l.chunk_id,
                ground_truth_id=w_l.ground_truth_id,
                is_seam_resolved=True,
                seam_id=seam_id,
                resolution_note=f"Retained left-only word '{w_l.text}'"
            ))
            details.append(f"RETAINED_LEFT: '{w_l.text}' at {w_l.start:.3f}s")
        elif w_r and not w_l:
            # Word only in right chunk
            if bias == "anti-duplication":
                shadowed = any(abs(w_r.start - mt) < 0.20 for mt in matched_left_times)
                if shadowed:
                    suppressed_count += 1
                    details.append(f"SUPPRESSED_RIGHT: '{w_r.text}' at {w_r.start:.3f}s (shadowed duplicate)")
                    continue
            retained_count += 1
            reconciled_words.append(Word(
                text=w_r.text,
                start=w_r.start,
                end=w_r.end,
                chunk_id=w_r.chunk_id,
                ground_truth_id=w_r.ground_truth_id,
                is_seam_resolved=True,
                seam_id=seam_id,
                resolution_note=f"Retained right-only word '{w_r.text}'"
            ))
            details.append(f"RETAINED_RIGHT: '{w_r.text}' at {w_r.start:.3f}s")
        else:
            # Mismatch between w_l and w_r (different tokens aligned)
            # In anti-duplication, keep earlier left token then right token if not overlapping
            if bias == "anti-duplication":
                if w_l:
                    reconciled_words.append(w_l)
                if w_r and w_l and (w_r.start > w_l.end):
                    reconciled_words.append(w_r)
            else:
                if w_l:
                    reconciled_words.append(w_l)
                if w_r:
                    reconciled_words.append(w_r)
                    
    # Sort reconciled overlap words by timestamp
    reconciled_words.sort(key=lambda w: w.start)
    
    t_start = min((w.start for w in words_left), default=overlap_start)
    t_end = max((w.end for w in words_right), default=overlap_end)
    
    return SeamResult(
        seam_id=seam_id,
        left_chunk_id=c_left_id,
        right_chunk_id=c_right_id,
        overlap_start=t_start,
        overlap_end=t_end,
        aligned_count=aligned_count,
        suppressed_count=suppressed_count,
        retained_count=retained_count,
        words=reconciled_words,
        details=details,
        margin=margin
    )


class TranscriptReconciler:
    """
    Maintains the state of received chunks and assembles the continuous transcript.
    """
    def __init__(self, bias: str = "anti-duplication"):
        self.bias = bias
        self.received_chunks: Dict[int, Dict[str, Any]] = {}  # chunk_id -> chunk dict
        self.raw_chunk_words: Dict[int, List[Word]] = {}      # chunk_id -> List[Word]
        self.resolved_seams: Dict[Tuple[int, int], SeamResult] = {}
        self._cached_transcript: List[Word] = []
        self._is_dirty: bool = False
        self.expected_chunks: Optional[int] = None
        
        # Metrics
        self.total_packets_received = 0
        self.successful_chunks_received = 0
        self.failed_chunks_received = 0
        self.duplicate_chunks_ignored = 0
        
    def reset(self, bias: Optional[str] = None):
        if bias:
            self.bias = bias
        self.received_chunks.clear()
        self.raw_chunk_words.clear()
        self.resolved_seams.clear()
        self._cached_transcript = []
        self._is_dirty = False
        self.expected_chunks = None
        self.total_packets_received = 0
        self.successful_chunks_received = 0
        self.failed_chunks_received = 0
        self.duplicate_chunks_ignored = 0

    def ingest_packet(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ingests a single packet received from the socket.
        Returns a status dictionary detailing what occurred.
        """
        self.total_packets_received += 1
        
        cid = packet.get("chunk_id")
        is_failure = packet.get("is_failure", False)
        
        if is_failure:
            self.failed_chunks_received += 1
            return {
                "status": "failure_logged",
                "chunk_id": cid,
                "message": f"Chunk {cid} reported ASR failure. Waiting for re-send."
            }
            
        # Check for duplicates
        if cid in self.received_chunks:
            self.duplicate_chunks_ignored += 1
            return {
                "status": "duplicate_ignored",
                "chunk_id": cid,
                "message": f"Chunk {cid} already successfully ingested. Ignoring duplicate."
            }
            
        # Ingest new chunk
        self.successful_chunks_received += 1
        self.received_chunks[cid] = packet
        self._is_dirty = True
        
        # Convert words to Word objects
        words = []
        for idx, w in enumerate(packet.get("words", [])):
            words.append(Word(
                text=w["text"],
                start=w["start"],
                end=w["end"],
                chunk_id=cid,
                ground_truth_id=w.get("word_id"),
                word_index=idx
            ))
        words.sort(key=lambda x: x.start)
        self.raw_chunk_words[cid] = words
        
        # Check if adjacent seams can now be resolved
        newly_resolved_seams = []
        
        # Left seam: (cid - 1, cid)
        if (cid - 1) in self.received_chunks:
            seam = self._resolve_seam(cid - 1, cid)
            if seam:
                newly_resolved_seams.append(seam.seam_id)
                
        # Right seam: (cid, cid + 1)
        if (cid + 1) in self.received_chunks:
            seam = self._resolve_seam(cid, cid + 1)
            if seam:
                newly_resolved_seams.append(seam.seam_id)
                
        return {
            "status": "chunk_ingested",
            "chunk_id": cid,
            "word_count": len(words),
            "newly_resolved_seams": newly_resolved_seams
        }

    def _resolve_seam(self, c_left: int, c_right: int) -> Optional[SeamResult]:
        """Resolves the seam between adjacent chunk c_left and c_right."""
        key = (c_left, c_right)
        if key in self.resolved_seams:
            return self.resolved_seams[key]
            
        left_meta = self.received_chunks[c_left]
        right_meta = self.received_chunks[c_right]
        
        # Overlap interval: between right window start and left window end
        # E.g. right starts at 25.0s, left ends at 30.0s
        overlap_start = right_meta["window_start"]
        overlap_end = left_meta["window_end"]
        overlap_duration = max(0.0, overlap_end - overlap_start)
        
        # Derive safe margin dynamically from chunk metadata (safe zone min(overlap_duration, 0.5))
        margin = min(overlap_duration, 0.5)
        
        # Candidate overlap words using bisect O(log n) boundary lookup
        c_left_words = self.raw_chunk_words[c_left]
        idx_left = bisect.bisect_left(c_left_words, overlap_start - margin, key=lambda w: w.start)
        words_left = c_left_words[idx_left:]
        
        c_right_words = self.raw_chunk_words[c_right]
        idx_right = bisect.bisect_right(c_right_words, overlap_end + margin, key=lambda w: w.start)
        words_right = c_right_words[:idx_right]
        
        seam_result = align_overlap_words(
            words_left, words_right,
            bias=self.bias,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
            margin=margin
        )
        self.resolved_seams[key] = seam_result
        self._is_dirty = True
        return seam_result

    def build_full_transcript(self, force_rebuild: bool = False) -> List[Word]:
        """
        Assembles all currently available chunks into one ordered, continuous transcript.
        For consecutive contiguous chunks with resolved seams, uses the reconciled seam words.
        Caches the built transcript with an invalidation flag and uses bisect for O(log n)
        cutoff lookups.
        """
        if not self._is_dirty and not force_rebuild and self._cached_transcript:
            return self._cached_transcript
            
        if not self.received_chunks:
            self._cached_transcript = []
            self._is_dirty = False
            return self._cached_transcript
            
        sorted_chunk_ids = sorted(self.received_chunks.keys())
        
        # Find contiguous ranges of chunks
        # e.g. [[0, 1, 2], [5, 6], [10]]
        ranges = []
        cur_range = [sorted_chunk_ids[0]]
        for cid in sorted_chunk_ids[1:]:
            if cid == cur_range[-1] + 1:
                cur_range.append(cid)
            else:
                ranges.append(cur_range)
                cur_range = [cid]
        ranges.append(cur_range)
        
        full_transcript: List[Word] = []
        
        for ch_range in ranges:
            if len(ch_range) == 1:
                # Isolated chunk with no adjacent neighbors yet
                cid = ch_range[0]
                full_transcript.extend(self.raw_chunk_words[cid])
            else:
                # Contiguous chain of chunks with resolved seams
                # For chunk 0 to k:
                # Chunk 0 prefix (before overlap with 1)
                # + Seam(0, 1) words
                # + Chunk 1 middle (between overlap with 0 and overlap with 2)
                # + Seam(1, 2) words ...
                for idx, cid in enumerate(ch_range):
                    c_meta = self.received_chunks[cid]
                    words = self.raw_chunk_words[cid]
                    
                    if idx == 0:
                        # First chunk in range: emit words before right seam
                        next_cid = ch_range[idx + 1]
                        seam = self.resolved_seams.get((cid, next_cid))
                        if seam:
                            m = getattr(seam, 'margin', 0.5)
                            cutoff = seam.overlap_start - m
                            idx_cut = bisect.bisect_left(words, cutoff, key=lambda w: w.start)
                            full_transcript.extend(words[:idx_cut])
                            full_transcript.extend(seam.words)
                        else:
                            full_transcript.extend(words)
                    else:
                        prev_cid = ch_range[idx - 1]
                        prev_seam = self.resolved_seams.get((prev_cid, cid))
                        prev_m = getattr(prev_seam, 'margin', 0.5) if prev_seam else 0.5
                        left_cutoff = prev_seam.overlap_end + prev_m if prev_seam else c_meta["window_start"]
                        
                        has_next = (idx + 1 < len(ch_range))
                        if has_next:
                            next_cid = ch_range[idx + 1]
                            next_seam = self.resolved_seams.get((cid, next_cid))
                            next_m = getattr(next_seam, 'margin', 0.5) if next_seam else 0.5
                            right_cutoff = next_seam.overlap_start - next_m if next_seam else c_meta["window_end"]
                            
                            idx_l = bisect.bisect_left(words, left_cutoff, key=lambda w: w.start)
                            idx_r = bisect.bisect_left(words, right_cutoff, key=lambda w: w.start)
                            full_transcript.extend(words[idx_l:idx_r])
                            if next_seam:
                                full_transcript.extend(next_seam.words)
                        else:
                            # Last chunk in this range: emit words after left seam
                            idx_l = bisect.bisect_left(words, left_cutoff, key=lambda w: w.start)
                            full_transcript.extend(words[idx_l:])
                            
        # Final deduplication and monotonic timestamp sort
        # Ensure start times are non-decreasing
        full_transcript.sort(key=lambda w: w.start)
        self._cached_transcript = full_transcript
        self._is_dirty = False
        return full_transcript

    def get_stats(self, expected_chunks: Optional[int] = None) -> Dict[str, Any]:
        """
        Returns live statistics of the reconciler.
        Word count is reported from the cached transcript without triggering a rebuild.
        """
        if expected_chunks is not None:
            exp_chunks = expected_chunks
        elif self.expected_chunks is not None:
            exp_chunks = self.expected_chunks
        elif self.received_chunks:
            exp_chunks = max(self.received_chunks.keys()) + 1
        else:
            exp_chunks = 0

        expected_seams = max(0, exp_chunks - 1) if exp_chunks > 0 else 0
        seams_resolved_count = len(self.resolved_seams)
        
        total_aligned_words = sum(s.aligned_count for s in self.resolved_seams.values())
        total_suppressed_words = sum(s.suppressed_count for s in self.resolved_seams.values())
        total_retained_words = sum(s.retained_count for s in self.resolved_seams.values())
        
        # Report word count from cache; do not trigger a rebuild if already assembled
        if self._is_dirty and not self._cached_transcript and self.received_chunks:
            self.build_full_transcript()
        cached_word_count = len(self._cached_transcript)
        
        return {
            "bias": self.bias,
            "packets_received": self.total_packets_received,
            "chunks_successful": self.successful_chunks_received,
            "chunks_failed": self.failed_chunks_received,
            "duplicates_ignored": self.duplicate_chunks_ignored,
            "expected_chunks": exp_chunks,
            "chunks_complete_pct": round((self.successful_chunks_received / exp_chunks) * 100, 1) if exp_chunks else 0.0,
            "seams_resolved": seams_resolved_count,
            "expected_seams": expected_seams,
            "seams_complete_pct": round((seams_resolved_count / expected_seams) * 100, 1) if expected_seams else 0.0,
            "total_reconciled_words": cached_word_count,
            "seam_aligned_words": total_aligned_words,
            "seam_suppressed_words": total_suppressed_words,
            "seam_retained_words": total_retained_words
        }
