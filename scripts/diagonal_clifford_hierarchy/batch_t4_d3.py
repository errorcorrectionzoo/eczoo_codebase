#!/usr/bin/env python3
"""Batch run Algorithm 6 at t=4 on all distance-3 codes in the eczoo database.

Uses multiprocessing to parallelize across all available CPU cores.
Output: stabilizer_diagonal_transversal_hier4_d3.txt
"""

import sys
import io
import json
import glob
import contextlib
import multiprocessing
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src/qiskit_qec/codes/codebase/data/base/base_data"
CSSLO_DIR = Path(__file__).parent.parent.parent / "CSSLO"
T_LEVEL = 4


def _worker_init():
    sys.path.insert(0, str(CSSLO_DIR))
    sys.path.insert(0, str(Path(__file__).parent))


def run_one(args):
    """Worker: run depth_one_t at T_LEVEL for one code. Returns result dict or None."""
    code_id, code = args
    # k=0 codes have no logical qubits — nothing for Algorithm 6 to find
    if code.get("k", 0) == 0:
        return None
    try:
        # deferred imports so each worker imports once after fork
        import sys, io, contextlib
        from pathlib import Path
        sys.path.insert(0, str(CSSLO_DIR))
        sys.path.insert(0, str(Path(__file__).parent))
        from CSSLO import depth_one_t, CSSCode
        from run_alg6_stabilizer import stabilizer_to_css, format_op

        Sx, Lx, SX_xp, SZ_xp, LX_xp = stabilizer_to_css(code)
        SX_can, LX_can, _, _ = CSSCode(Sx, LX=Lx)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = depth_one_t(SX_can, LX_can, t=T_LEVEL)
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
            "d":       code.get("d", 3),
            "id":      code_id,
            "is_css":  code.get("is_css", 0),
            "phys":    format_op(phys_raw) if phys_raw else "(not captured)",
            "logical": format_op(target),
        }
    except Exception as e:
        return {"error": str(e), "id": code_id, "n": code.get("n"), "k": code.get("k", "?")}


def load_all_d3():
    """Load every d=3 code from the database. Returns list of (code_id, code_dict)."""
    pattern = str(DATA_DIR / "*/codes_n_*_k_*_d_3.json")
    items = []
    for fpath in sorted(glob.glob(pattern)):
        data = json.load(open(fpath))
        for key, code in data.items():
            items.append((int(key), code))
    return items


def main():
    items = load_all_d3()
    total = len(items)
    print(f"Loaded {total} d=3 codes. Running t={T_LEVEL} with {multiprocessing.cpu_count()} workers...",
          flush=True)

    hits = []
    errors = []
    done = 0

    with multiprocessing.Pool() as pool:
        for result in pool.imap_unordered(run_one, items, chunksize=4):
            done += 1
            if done % 200 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
            if result is None:
                continue
            if "error" in result:
                errors.append(result)
            else:
                hits.append(result)

    # Sort hits by (n, k, id)
    hits.sort(key=lambda r: (r["n"], r["k"], r["id"]))

    # --- build output ---
    k0 = sum(1 for _, c in items if c.get("k", 0) == 0)
    lines = [
        f"Diagonal transversal gates at hierarchy level t={T_LEVEL} for all d=3 stabilizer codes",
        f"Total codes: {total}  |  k=0 skipped: {k0}  |  tested: {total-k0}  |  hits: {len(hits)}  |  errors: {len(errors)}",
        f"Script: run_alg6_stabilizer.py  (CSS and non-CSS stabilizer codes)",
        "",
    ]

    # Group hits by (n, k)
    from itertools import groupby
    key_fn = lambda r: (r["n"], r["k"])
    for (n, k), group in groupby(hits, key=key_fn):
        group = list(group)
        lines.append(f"[[{n},{k},3]]")
        for r in group:
            kind = "CSS" if r["is_css"] else "non-CSS"
            lines.append(f"  id={r['id']:<6} ({kind})  physical={r['phys']}  logical={r['logical']}")
        lines.append("")

    if not hits:
        lines.append("No t=4 diagonal transversal gate found for any d=3 code.")
        lines.append("")

    if errors:
        lines.append(f"ERRORS ({len(errors)}):")
        for e in errors:
            lines.append(f"  [[{e['n']},{e['k']},3]] id={e['id']}: {e['error']}")
        lines.append("")

    # Summary table
    # Count totals per (n,k)
    from collections import defaultdict
    counts = defaultdict(lambda: {"total": 0, "css": 0, "hits": 0})
    for _, code in items:
        nk = (code["n"], code["k"])
        counts[nk]["total"] += 1
        counts[nk]["css"] += code.get("is_css", 0)
    for r in hits:
        counts[(r["n"], r["k"])]["hits"] += 1

    lines.append("SUMMARY")
    lines.append(f"  {'Code':<12} {'Total':>6}  {'CSS':>5}  {'non-CSS':>8}  {'t=4 hits':>8}")
    for (n, k) in sorted(counts):
        c = counts[(n, k)]
        ncss = c["total"] - c["css"]
        lines.append(f"  [[{n},{k},3]]  {c['total']:>6}  {c['css']:>5}  {ncss:>8}  {c['hits']:>8}")

    output = "\n".join(lines) + "\n"
    print(output)

    outfile = Path(__file__).parent / "stabilizer_diagonal_transversal_hier4_d3.txt"
    outfile.write_text(output)
    print(f"Written to {outfile}")


if __name__ == "__main__":
    main()
