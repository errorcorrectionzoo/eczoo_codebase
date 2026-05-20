#!/usr/bin/env python3
"""Batch run Algorithm 6 on eczoo stabilizer codes (CSS and non-CSS).

Searches for diagonal transversal gates at Clifford-hierarchy level t.
Uses multiprocessing to parallelize across available CPU cores.

Usage:
    python batch_alg6.py [options]

Filter options (all combinable):
    --n N          restrict to n physical qubits (repeatable: --n 8 --n 9)
    --k K          restrict to k logical qubits  (repeatable; k=0 always skipped)
    --d D          restrict to distance d         (repeatable)
    --n-max N      restrict to n <= N
    --d-min D      restrict to d >= D
    --k-min K      restrict to k >= K (default: 1, skips k=0)

Algorithm option:
    -t T           Clifford hierarchy level to search (default: 3)

Output:
    -o FILE        output file path (default: stdout + auto-named file in same dir)
"""

import sys
import io
import json
import glob
import argparse
import contextlib
import multiprocessing
from collections import defaultdict
from itertools import groupby
from pathlib import Path

_SCRIPT_DIR  = Path(__file__).resolve().parent
DATA_DIR     = _SCRIPT_DIR.parent.parent / "src/qiskit_qec/codes/codebase/data/base/base_data"
CSSLO_DIR    = _SCRIPT_DIR.parent.parent.parent / "CSSLO"

# Set up sys.path at module level so forked workers inherit the entries and
# any already-imported modules from the main process.
for _p in (str(CSSLO_DIR), str(_SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from CSSLO import depth_one_t, CSSCode
from run_alg6_stabilizer import stabilizer_to_css, format_op


def run_one(args):
    """Worker: run depth_one_t at t_level for one code. Returns result dict or None."""
    code_id, code, t_level = args
    if code.get("k", 0) == 0:
        return None
    try:
        Sx, Lx, SX_xp, SZ_xp, LX_xp = stabilizer_to_css(code)
        SX_can, LX_can, _, _ = CSSCode(Sx, LX=Lx)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = depth_one_t(SX_can, LX_can, t=t_level)
        csslo_out = buf.getvalue()

        if result is None:
            return None

        _, target = result
        phys_raw = next(
            (l.split(":", 1)[1].strip() for l in csslo_out.splitlines() if "Logical Operator:" in l),
            None,
        )
        return {
            "n":       code["n"],
            "k":       code["k"],
            "d":       code.get("d", "?"),
            "id":      code_id,
            "is_css":  code.get("is_css", 0),
            "phys":    format_op(phys_raw) if phys_raw else "(not captured)",
            "logical": format_op(target),
        }
    except Exception as e:
        return {
            "error": str(e),
            "id":    code_id,
            "n":     code.get("n", "?"),
            "k":     code.get("k", "?"),
            "d":     code.get("d", "?"),
        }


def load_codes(ns, ks, ds, n_max, k_min, d_min=None):
    """Load matching codes from the database. Returns list of (code_id, code_dict)."""
    pattern = str(DATA_DIR / "*/codes_n_*_k_*_d_*.json")
    items = []
    for fpath in sorted(glob.glob(pattern)):
        parts = Path(fpath).stem.split("_")   # codes_n_N_k_K_d_D
        fn, fk, fd = int(parts[2]), int(parts[4]), int(parts[6])
        if ns              and fn not in ns:   continue
        if ks              and fk not in ks:   continue
        if ds              and fd not in ds:   continue
        if n_max is not None and fn > n_max:   continue
        if d_min is not None and fd < d_min:   continue
        if fk < k_min:                         continue
        data = json.load(open(fpath))
        for key, code in data.items():
            k = code.get("k", 0)
            if k < k_min:
                continue
            if ks and k not in ks:
                continue
            items.append((int(key), code))
    return items


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n",     type=int, action="append", dest="ns",  metavar="N")
    parser.add_argument("--k",     type=int, action="append", dest="ks",  metavar="K")
    parser.add_argument("--d",     type=int, action="append", dest="ds",  metavar="D")
    parser.add_argument("--n-max", type=int, default=None)
    parser.add_argument("--d-min", type=int, default=None)
    parser.add_argument("--k-min", type=int, default=1)
    parser.add_argument("-t",      type=int, default=3)
    parser.add_argument("-o",      type=Path, default=None)
    args = parser.parse_args()

    ns    = set(args.ns) if args.ns else set()
    ks    = set(args.ks) if args.ks else set()
    ds    = set(args.ds) if args.ds else set()

    items = load_codes(ns, ks, ds, args.n_max, args.k_min, args.d_min)
    total = len(items)
    if total == 0:
        print("No codes matched the given filters.", file=sys.stderr)
        sys.exit(1)

    ncpu = multiprocessing.cpu_count()
    print(f"Loaded {total} codes. Running t={args.t} with {ncpu} workers...", flush=True)

    work   = [(cid, code, args.t) for cid, code in items]
    hits   = []
    errors = []
    done   = 0

    with multiprocessing.Pool() as pool:
        for result in pool.imap_unordered(run_one, work, chunksize=4):
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
            if result is None:
                continue
            if "error" in result:
                errors.append(result)
            else:
                hits.append(result)

    hits.sort(key=lambda r: (r["n"], r["k"], r["d"], r["id"]))

    # --- filter description for header ---
    def fmt_filter(label, vals):
        return f"{label}={','.join(str(v) for v in sorted(vals))}" if vals else None
    filters = [x for x in [fmt_filter("n", ns), fmt_filter("k", ks), fmt_filter("d", ds),
                            f"n_max={args.n_max}" if args.n_max else None,
                            f"d_min={args.d_min}" if args.d_min else None,
                            f"k_min={args.k_min}" if args.k_min > 1 else None] if x]
    filter_str = "  filters: " + ", ".join(filters) if filters else "  filters: none (all codes)"

    k0 = sum(1 for _, c in items if c.get("k", 0) == 0)
    lines = [
        f"Diagonal transversal gates at hierarchy level t={args.t}",
        filter_str,
        f"Total codes: {total}  |  k=0 skipped: {k0}  |  tested: {total-k0}"
        f"  |  hits: {len(hits)}  |  errors: {len(errors)}",
        f"Script: run_alg6_stabilizer.py  (CSS and non-CSS stabilizer codes)",
        "",
    ]

    for (n, k, d), group in groupby(hits, key=lambda r: (r["n"], r["k"], r["d"])):
        group = list(group)
        lines.append(f"[[{n},{k},{d}]]")
        for r in group:
            kind = "CSS" if r["is_css"] else "non-CSS"
            lines.append(f"  id={r['id']:<6} ({kind})  physical={r['phys']}  logical={r['logical']}")
        lines.append("")

    if not hits:
        lines.append(f"No t={args.t} diagonal transversal gate found.")
        lines.append("")

    if errors:
        lines.append(f"ERRORS ({len(errors)}):")
        for e in errors:
            lines.append(f"  [[{e['n']},{e['k']},{e['d']}]] id={e['id']}: {e['error']}")
        lines.append("")

    # Summary table
    counts = defaultdict(lambda: {"total": 0, "css": 0, "hits": 0})
    for _, code in items:
        key = (code["n"], code["k"], code.get("d", "?"))
        counts[key]["total"] += 1
        counts[key]["css"]   += code.get("is_css", 0)
    for r in hits:
        counts[(r["n"], r["k"], r["d"])]["hits"] += 1

    lines.append("SUMMARY")
    lines.append(f"  {'Code':<14} {'Total':>6}  {'CSS':>5}  {'non-CSS':>8}  {'t=%d hits' % args.t:>9}")
    for (n, k, d) in sorted(counts):
        c = counts[(n, k, d)]
        ncss = c["total"] - c["css"]
        lines.append(f"  [[{n},{k},{d}]]  {c['total']:>6}  {c['css']:>5}  {ncss:>8}  {c['hits']:>9}")

    output = "\n".join(lines) + "\n"
    print(output)

    outfile = args.o
    if outfile is None:
        tag = "_".join(filters).replace("=", "").replace(",", "-") or "all"
        outfile = Path(__file__).parent / f"stabilizer_diagonal_transversal_hier{args.t}_{tag}.txt"
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(output)
    print(f"Written to {outfile}")


if __name__ == "__main__":
    main()
