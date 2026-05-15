#!/usr/bin/env python3
"""Batch-compute the optimal LC Lee distance for all codes in the base_data database.

For each code with k >= 1, performs an exhaustive 3^n LC search (excluding H gates,
which never affect Lee distance) and injects two new fields into the JSON:
  "d_lee":          best Lee distance over all LC-equivalents
  "lee_generators": generators of the LC-equivalent achieving that distance

Both fields are placed immediately after their sibling keys ("d" and
"isotropic_generators" respectively), preserving the existing key order.

Resumability
------------
Progress is tracked via a sidecar file <filename>.delta.jsonl next to each JSON.
Each line records one processed code:
  {"id": "4396", "d_lee": 5, "lee_generators": ["Y0Y8", ...]}

Appending a line is O(1) regardless of file size.  The main JSON is rebuilt from
the sidecar every --flush-every codes (default 200).  On restart the sidecar is
read first so already-computed codes are skipped.  When a file is fully done the
sidecar is deleted.

Usage
-----
  python compute_lee_database.py [--flush-every N] [--start-n N] [--start-k N]
"""

import argparse
import json
import sys
import time
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lee_distance import generators_to_H, lee_distance
from max_lee_distance import apply_lc, H_to_generators

DATA_DIR = (
    Path(__file__).parent.parent
    / "src/qiskit_qec/codes/codebase/data/base/base_data"
)


# ---------------------------------------------------------------------------
# Core LC optimisation (quiet — no print statements)
# ---------------------------------------------------------------------------

def compute_max_lee(H_rows: list[int], n: int, d_pauli: int) -> tuple[int, list[int]]:
    """Return (best_lee_dist, best_H_rows) over all 3^n LC-equivalents."""
    upper = 2 * d_pauli
    best_dist = lee_distance(H_rows, n, max_weight=upper)
    best_H = list(H_rows)
    if best_dist >= upper:
        return best_dist, best_H
    for ops in product(range(3), repeat=n):
        if ops == (0,) * n:
            continue
        H_lc = apply_lc(H_rows, n, ops)
        if lee_distance(H_lc, n, max_weight=best_dist) != 0:
            continue  # can't beat current best
        d = lee_distance(H_lc, n, max_weight=upper)
        if d > best_dist:
            best_dist = d
            best_H = H_lc
            if best_dist >= upper:
                break
    return best_dist, best_H


# ---------------------------------------------------------------------------
# JSON / sidecar helpers
# ---------------------------------------------------------------------------

def _insert_after(entry: dict, after_key: str, new_key: str, new_value) -> dict:
    """Return a new dict with new_key inserted immediately after after_key."""
    out: dict = {}
    for k, v in entry.items():
        out[k] = v
        if k == after_key and new_key not in entry:
            out[new_key] = new_value
    return out


def _save_json(path: Path, data: dict) -> None:
    """Atomically write data to path as indented JSON."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    tmp.replace(path)


def _load_delta(delta_path: Path) -> dict[str, dict]:
    """Read the sidecar; return {code_id: {"d_lee": int, "lee_generators": list}}."""
    done: dict[str, dict] = {}
    if not delta_path.exists():
        return done
    with open(delta_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                done[rec["id"]] = {
                    "d_lee": rec["d_lee"],
                    "lee_generators": rec["lee_generators"],
                }
    return done


def _append_delta(delta_path: Path, code_id: str, d_lee: int, lee_gens: list[str]) -> None:
    with open(delta_path, "a") as f:
        f.write(
            json.dumps({"id": code_id, "d_lee": d_lee, "lee_generators": lee_gens}) + "\n"
        )


def _flush_delta(json_path: Path, codes: dict, delta: dict[str, dict]) -> dict:
    """Inject all delta entries into codes and save the main JSON."""
    for code_id, fields in delta.items():
        entry = codes[code_id]
        if "d_lee" not in entry:
            entry = _insert_after(entry, "d", "d_lee", fields["d_lee"])
        else:
            entry["d_lee"] = fields["d_lee"]
        if "lee_generators" not in entry:
            entry = _insert_after(
                entry, "isotropic_generators", "lee_generators", fields["lee_generators"]
            )
        else:
            entry["lee_generators"] = fields["lee_generators"]
        codes[code_id] = entry
    _save_json(json_path, codes)
    return codes


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(json_path: Path, n: int, flush_every: int) -> tuple[int, int]:
    """Process all pending codes in one JSON file.

    Returns (n_processed, n_skipped).
    Raises KeyboardInterrupt (after saving) if the user aborts.
    """
    delta_path = json_path.with_suffix(".delta.jsonl")

    with open(json_path) as f:
        codes = json.load(f)

    if not codes:
        return 0, 0

    delta = _load_delta(delta_path)

    # Pending = k>=1, not in delta, not already in the JSON
    pending = [
        cid for cid, entry in codes.items()
        if entry.get("k", 0) >= 1
        and cid not in delta
        and ("d_lee" not in entry or "lee_generators" not in entry)
    ]
    n_skipped = len(codes) - len(pending)

    if not pending:
        if delta:
            _flush_delta(json_path, codes, delta)
            delta_path.unlink(missing_ok=True)
        return 0, n_skipped

    processed = 0
    unflushed = 0
    t_file = time.perf_counter()

    try:
        for i, cid in enumerate(pending):
            entry = codes[cid]
            d_pauli = entry["d"]

            t0 = time.perf_counter()
            H_rows = generators_to_H(entry["isotropic_generators"], n)
            d_lee, best_H = compute_max_lee(H_rows, n, d_pauli)
            lee_gens = H_to_generators(best_H, n)
            elapsed = time.perf_counter() - t0

            _append_delta(delta_path, cid, d_lee, lee_gens)
            delta[cid] = {"d_lee": d_lee, "lee_generators": lee_gens}

            processed += 1
            unflushed += 1

            total_elapsed = time.perf_counter() - t_file
            eta_s = total_elapsed / processed * (len(pending) - i - 1)
            flag = "*" if d_lee > d_pauli else " "
            print(
                f"  {flag}[{i + 1}/{len(pending)}] id={cid}"
                f"  d={d_pauli}  d_lee={d_lee}"
                f"  {elapsed:.2f}s  ETA {eta_s / 60:.0f}m",
                flush=True,
            )

            if unflushed >= flush_every:
                codes = _flush_delta(json_path, codes, delta)
                unflushed = 0

    except KeyboardInterrupt:
        if unflushed:
            print("\n  Interrupted — flushing to JSON...", flush=True)
            _flush_delta(json_path, codes, delta)
        raise

    if unflushed:
        codes = _flush_delta(json_path, codes, delta)

    delta_path.unlink(missing_ok=True)
    return processed, n_skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-compute optimal LC Lee distances for the qiskit-qec database."
    )
    parser.add_argument(
        "--flush-every", type=int, default=200,
        help="Rebuild and save main JSON every N processed codes (default: 200).",
    )
    parser.add_argument(
        "--start-n", type=int, default=1,
        help="Skip files with n < this value (default: 1).",
    )
    parser.add_argument(
        "--start-k", type=int, default=1,
        help="Skip files with k < this value (default: 1, skipping k=0 codes).",
    )
    args = parser.parse_args()

    files = sorted(
        DATA_DIR.glob("n_*/codes_n_*_k_*.json"),
        key=lambda p: (
            int(p.parent.name.split("_")[1]),
            int(p.stem.split("_k_")[1]),
        ),
    )
    files = [
        p for p in files
        if int(p.parent.name.split("_")[1]) >= args.start_n
        and int(p.stem.split("_k_")[1]) >= args.start_k
    ]

    print(f"Database : {DATA_DIR}")
    print(f"Files    : {len(files)}  (flush every {args.flush_every} codes, k >= {args.start_k})")
    print()

    total_processed = 0
    total_skipped = 0

    try:
        for path in files:
            n = int(path.parent.name.split("_")[1])
            k = int(path.stem.split("_k_")[1])
            size_kb = path.stat().st_size // 1024

            with open(path) as f:
                codes_peek = json.load(f)
            if not codes_peek:
                continue

            delta = _load_delta(path.with_suffix(".delta.jsonl"))
            pending = [
                cid for cid, e in codes_peek.items()
                if e.get("k", 0) >= 1
                and cid not in delta
                and ("d_lee" not in e or "lee_generators" not in e)
            ]
            done_count = len(codes_peek) - len(pending)

            label = f"[n={n} k={k}]  {len(codes_peek)} codes  {size_kb} KB"
            if not pending:
                print(f"{label}  — all done, skipping.")
                total_skipped += len(codes_peek)
                continue

            print(f"{label}  — {len(pending)} pending  ({done_count} already done)")

            n_proc, n_skip = process_file(path, n, args.flush_every)
            total_processed += n_proc
            total_skipped += n_skip
            print(f"  File done: {n_proc} processed, {n_skip} skipped.\n", flush=True)

    except KeyboardInterrupt:
        print(f"\nStopped. Processed {total_processed} codes this run.")
        sys.exit(0)

    print(f"Finished. Processed {total_processed} codes, skipped {total_skipped} already done.")


if __name__ == "__main__":
    main()
