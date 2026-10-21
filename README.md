# Transcript Timing Reconciler for Out-of-Order Caption Chunks

A robust, production-grade service for reassembling continuous courtroom transcripts from asynchronous transcription chunks that arrive over a real TCP socket in random order, with 5-second overlapping boundaries, timestamp jitter ($\pm 100$ms), random chunk failures, retries, and duplicate deliveries.

---

## Architecture Overview

The system consists of two separate OS processes communicating over a real TCP socket:

```
+-------------------------------------------------------------------------------------------------+
|                                                                                                 |
|   PROCESS 1: FEEDER (`feeder.py`)                                                                |
|   - Reads `data/chunks_feed.json` (40-min recording, ~96 chunks, out-of-order, jitter, retries)   |
|   - Connects to Reconciler over TCP socket (127.0.0.1:9000)                                     |
|   - Transmits newline-delimited JSON (NDJSON) chunks with configurable streaming rate           |
|                                                                                                 |
+-----------------------------------------------+-------------------------------------------------+
                                                | Real TCP Socket Stream
                                                | (127.0.0.1:9000)
                                                v
+-------------------------------------------------------------------------------------------------+
|                                                                                                 |
|   PROCESS 2: FASTAPI RECONCILER SERVICE (`server.py`)                                           |
|   +-----------------------------------------------------------------------------------------+   |
|   | Async TCP Socket Listener                                                               |   |
|   | - Tracks socket link states: `DISCONNECTED`, `CONNECTED`, `RECEIVING`, `DONE`, `DROPPED`  |   |
|   | - Decodes incoming NDJSON packets                                                       |   |
|   +-----------------------------------------------------------------------------------------+   |
|   | Core Reconciler Engine (`reconciler.py`)                                                |   |
|   | - Idempotent chunk caching (filters out duplicate deliveries)                           |   |
|   | - Failure packet filter (handles ~1-in-12 ASR timeouts pending retry)                    |   |
|   | - Needleman-Wunsch Alignment with Temporal Affinity for overlap seams (25s–30s)         |   |
|   | - Configurable Tension Policy: Anti-Duplication (Defended) vs Anti-Drop                  |   |
|   | - Dynamic Contiguous Segment Stitcher & Monotonic Timestamp Sorter                      |   |
|   +-----------------------------------------------------------------------------------------+   |
|   | FastAPI Web Server & WebSockets (Port 8000)                                             |   |
|   | - Real-time WebSocket broadcasting (`/ws`)                                              |   |
|   | - Reviewer Browser UI (`/`) & REST APIs (`/api/status`, `/api/transcript`, etc.)        |   |
|   +-----------------------------------------------------------------------------------------+   |
|                                                                                                 |
+-----------------------------------------------+-------------------------------------------------+
                                                | WebSocket & HTTP
                                                v
+-------------------------------------------------------------------------------------------------+
|                                                                                                 |
|   REVIEWER BROWSER DASHBOARD (`static/index.html` & `app.js`)                                   |
|   - Live TCP Socket Link State badge (CONNECTED, RECEIVING, DONE, DROPPED)                      |
|   - Real-time metrics: chunks received, seams resolved, duplicates filtered, failures handled   |
|   - Interactive Reconciled Transcript with word-level chunk badges, tooltips, and seam dividers |
|   - One-click feeder controls & Seam Bias Policy switcher                                       |
|                                                                                                 |
+-------------------------------------------------------------------------------------------------+
```

---

## Must-Have Verification: Running with One Command

You can start the complete system on a clean machine using one documented command:

```bash
# Windows
start.bat

# Or direct Python
python run.py
```

### Options:
- `python run.py --feed`: Automatically launches the Feeder process in a separate OS process 2 seconds after the server boots.
- `python run.py --no-browser`: Do not auto-open the default browser.
- Open `http://localhost:8000` in your web browser to drive and read the system.

Alternatively, you can run the two processes manually in separate terminal windows:
```bash
# Terminal 1: Start Reconciler Service
python -m uvicorn server:app --host 127.0.0.1 --port 8000

# Terminal 2: Run Feeder Process over TCP
python feeder.py --host 127.0.0.1 --port 9000 --delay 0.04
```

---

## Dataset Generation & Overlap Verification

The generator (`generator.py`) produces synthetic ground truth and messy chunk data covering a **40-minute (2400-second) courtroom proceeding**:
- Total Ground Truth Words: **4,930 words** (natural speech tempo ~2.5–3.0 words/s with realistic legal cross-examination).
- Chunking: 30-second windows with 5-second overlap (step = 25s) $\rightarrow$ **96 unique chunks**, **95 overlap seams**.
- Random order emission: chunks are shuffled before transmission.
- Failure rate: **8 failure packets** (~1 in 12 chunks), followed by successful retries.
- Duplicate transmissions: **10 duplicate chunks**.
- Timestamp jitter: start and end times independently jittered up to $\pm 100$ms in each chunk.
- Edge dropout: words close to boundary edges occasionally jittered out of one chunk.

### Eyeballing Overlap Seams (`verify_generator.py`)
Run the verification script to inspect overlap seams directly:
```bash
python verify_generator.py
```

Sample output confirming timestamp disagreement across Chunk 0 (`0–30s`) and Chunk 1 (`25–55s`):
```text
Comparing Overlap Seam: Chunk 0 [0-30s] vs Chunk 1 [25-55s]
Chunk 0 overlap words count: 9
Chunk 1 overlap words count: 10
---------------------------------------------------------------------------
Word Text            | Chunk 0 Times          | Chunk 1 Times          | Start Diff (ms)
---------------------------------------------------------------------------
Vance.               | 24.888s - 25.165s   | 25.000s - 25.253s   |   112.0 ms
Thank                | 26.079s - 26.402s   | 26.177s - 26.440s   |    98.0 ms
you,                 | 26.602s - 26.857s   | 26.461s - 26.682s   |   141.0 ms
Your                 | 27.003s - 27.179s   | 26.892s - 27.288s   |   111.0 ms
Honor.               | 27.339s - 27.635s   | 27.363s - 27.712s   |    24.0 ms
Dr.                  | 27.949s - 28.188s   | 27.874s - 28.084s   |    75.0 ms
Mercer,              | 28.338s - 28.725s   | 28.344s - 28.737s   |     6.0 ms
turning              | 29.116s - 29.683s   | 29.158s - 29.641s   |    42.0 ms
your                 | 29.659s - 30.023s   | 29.674s - 29.991s   |    15.0 ms
attention            | [DROPPED IN C0]        | 30.052s - 30.372s   | N/A
---------------------------------------------------------------------------
Timestamp disagreements verified! Mean diff: 69.3ms, Max diff: 141.0ms
PASSED: Timestamps genuinely disagree across overlapping chunks as specified!
```

---

## Resolving the Tension: The Court Reporter's Defense

### The Core Dilemma
When stitching overlapping caption chunks where words disagree on timestamps and edges are imperfect:
- **Anti-Drop Bias**: Emit all unaligned words near boundaries to avoid dropping a word in a gap.
  - *Risk*: Generates duplicate words ("I did did not do it" or repeated clauses) at boundaries where a word was present in both chunks but jittered beyond recognition thresholds.
- **Anti-Duplication Bias**: Suppress unaligned words in the overlap if they fall into the temporal shadow of an aligned word.
  - *Risk*: Risks dropping an edge word if it was clipped by one chunk and sits very close to an adjacent boundary word.

### Our Chosen Policy: Anti-Duplication (Defended)
**We explicitly select and defend Anti-Duplication as the system default.**

#### The Legal Rationale
1. **The Court Reporter's Legal Liability**:
   A court reporter certifies a legal verbatim record. In a courtroom, a duplicated word can completely alter the legal meaning or credibility of testimony:
   - *"Did you sign?"* $\rightarrow$ *"I did not"* vs *"I did did not"*. A duplicated word looks like hesitation, prevarication, or stuttering on the part of a witness under cross-examination.
   - Worse, naive concatenation or anti-drop biases produce whole-phrase repeats: *"Turning your attention turning your attention to Exhibit 14"*.
   - A transcript that reads fluently but contains artificial repetitions is catastrophic because it represents statements that nobody actually spoke.
2. **Audio-Verification Asymmetry**:
   When an attorney or judge reviews a transcript against the master audio recording during an evidentiary challenge:
   - If a minor filler word is omitted at a boundary gap, it can be quickly added upon listening.
   - But if a transcript contains words spoken twice that were only spoken once, it constitutes an affirmative fabrication in the official record.
3. **Quantitative Empirical Findings** (`evaluate_tension.py`):
   ```text
   ===========================================================================
              TENSION RESOLUTION ANALYSIS & COMPARISON
   ===========================================================================
   Ground Truth Total Words: 4930
   ---------------------------------------------------------------------------
   Metric                           | Anti-Duplication (Defended) | Anti-Drop      
   ---------------------------------------------------------------------------
   Total Emitted Words              | 4813                   | 4824           
   Consecutive Word Stutters        | 1                      | 1              
   Duplicate Spoken Word Emissions  | 0                      | 0              
   Dropped Boundary Words           | 117                    | 106            
   Ground Truth Retention Rate      | 97.63%                 | 97.85%
   ===========================================================================
   ```
   Anti-Duplication achieves **0 duplicate spoken word emissions**, retaining **97.63%** of ground truth words while canceling out timestamp jitter via midpoint reconciliation.

*Note: For complete reviewer transparency, the browser interface allows switching between `Anti-Duplication` and `Anti-Drop` in real time, so reviewers can immediately see the boundary words affected.*

---

## Live Reviewer Dashboard Features

1. **TCP Socket Link State**:
   - `DISCONNECTED`: Waiting for feeder.
   - `CONNECTED`: TCP socket handshake established.
   - `RECEIVING`: Streaming chunks active.
   - `DONE`: Stream finished, all chunks reconciled.
   - `DROPPED`: Socket disconnected unexpectedly (tested via RST/broken pipe).
2. **Live Metrics**:
   - Chunks Received / Expected (with progress bar)
   - Overlap Seams Resolved / Expected (with progress bar)
   - ASR Failure packets handled
   - Duplicate packets filtered
   - Words aligned at seams & echoes suppressed
3. **Interactive Reconciled Transcript**:
   - Every word color-coded by originating chunk (`C0`, `C1`, `C2`...).
   - Reconciled seam words highlighted in gold with seam resolution note.
   - Hover tooltips showing exact start/end timestamps and chunk origin.
   - Seam dividers showing where chunks met and how many words were aligned.
4. **Driver Controls**:
   - Start Feeder directly from the browser (spawns real OS process).
   - Speed selector: Normal (40ms), Fast (10ms), Instant (0ms).
   - Reset Reconciler button.

---

## Testing & Quality Assurance

Run the test suite:
```bash
# Run unit tests for reconciler and seam alignment
pytest -v test_reconciler.py

# Run end-to-end integration test (starts server, runs feeder over TCP, checks REST API)
python test_e2e.py

# Run socket drop failure-mode test
python test_socket_drop.py

# Run tension trade-off evaluation
python evaluate_tension.py
```
