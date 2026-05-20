#!/usr/bin/env python3
"""Compute the Lee distance of a code from the qiskit-qec codebase.

Usage: python lee_distance.py <n> <k> <id> [--max-weight N]

The Lee weight of a Pauli X^a Z^b on n qubits is wt(a) + wt(b),
giving I=0, X=1, Z=1, Y=2. Lee distance is the minimum Lee weight
of any logical operator (element of N(S) \\ S).
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src/qiskit_qec/codes/codebase/data/base/base_data"

_PAULI_RE = re.compile(r"([XYZ])(\d+)")
_PAULI_XZ = {"X": (1, 0), "Y": (1, 1), "Z": (0, 1)}


def load_code(n: int, k: int, code_id: int) -> dict:
    path = DATA_DIR / f"n_{n}" / f"codes_n_{n}_k_{k}.json"
    with open(path) as f:
        codes = json.load(f)
    key = str(code_id)
    if key not in codes:
        sys.exit(f"Error: code ID {code_id} not found in [[{n},{k}]] database.")
    return codes[key]


def generators_to_H(generators: list[str], n: int) -> list[int]:
    """Parse compact Pauli strings to a list of row integers (2n-bit each).

    Bit layout: bit j = x_j for j < n, bit j+n = z_j for j in 0..n-1.
    """
    rows = []
    for gen in generators:
        row = 0
        for m in _PAULI_RE.finditer(gen):
            pauli, q = m.group(1), int(m.group(2))
            x, z = _PAULI_XZ[pauli]
            if x:
                row |= 1 << q
            if z:
                row |= 1 << (q + n)
        rows.append(row)
    return rows


def _nc_col_ints(H_rows: list[int], n: int) -> list[int]:
    """Compute column integers for the normalizer condition matrix NC = [Hz | Hx].

    For a vector v = [v_x | v_z], v is in N(S) iff NC @ v = 0 mod 2.
    The j-th column of NC is:
      j < n  -> column j of Hz  (bits n..2n-1 of H shifted down)
      j >= n -> column j-n of Hx (bits 0..n-1 of H)
    Each column is an m-bit integer encoding contributions from each generator.
    """
    m = len(H_rows)
    N = 2 * n
    col_ints = [0] * N
    for i, row in enumerate(H_rows):
        for j in range(n):
            # NC column j (x-error position j) <- Hz column j = z-bit of qubit j
            if (row >> (j + n)) & 1:
                col_ints[j] |= 1 << i
            # NC column n+j (z-error position j) <- Hx column j = x-bit of qubit j
            if (row >> j) & 1:
                col_ints[n + j] |= 1 << i
    return col_ints


def _row_reduce(H_rows: list[int], N: int) -> tuple[list[int], dict[int, int]]:
    """Row reduce H over F_2 (rows as N-bit integers). Returns (rref, pivot_map)."""
    rref = list(H_rows)
    m = len(rref)
    pivot_map: dict[int, int] = {}  # bit position -> row index
    r = 0
    for c in range(N):
        mask = 1 << c
        found = next((rr for rr in range(r, m) if rref[rr] & mask), -1)
        if found == -1:
            continue
        rref[r], rref[found] = rref[found], rref[r]
        for rr in range(m):
            if rr != r and rref[rr] & mask:
                rref[rr] ^= rref[r]
        pivot_map[c] = r
        r += 1
    return rref, pivot_map


def _is_logical(v_int: int, rref: list[int], pivot_map: dict[int, int]) -> bool:
    """Return True if v (as int) is in N(S) but not in rowspan(H)."""
    for bit, row in pivot_map.items():
        if v_int & (1 << bit):
            v_int ^= rref[row]
    return v_int != 0


def lee_distance(H_rows: list[int], n: int, max_weight: int = 10) -> int:
    """Find the minimum Lee weight logical operator using meet-in-the-middle.

    Returns the Lee distance, or 0 if no logical found within max_weight.
    """
    N = 2 * n
    col_ints = _nc_col_ints(H_rows, n)
    rref, pivot_map = _row_reduce(H_rows, N)

    for weight in range(1, max_weight + 1):
        w1 = weight // 2
        w2 = weight - w1

        # Build left table: syndrome -> list of (position tuple, v_int)
        left: dict[int, list[tuple]] = defaultdict(list)
        if w1 == 0:
            left[0].append(((), 0))
        else:
            for combo in combinations(range(N), w1):
                syn = 0
                v = 0
                for p in combo:
                    syn ^= col_ints[p]
                    v |= 1 << p
                left[syn].append((combo, v))

        # Sweep right side, look up matching syndromes
        right_iter = [((), 0)] if w2 == 0 else (
            (combo, sum(1 << p for p in combo))
            for combo in combinations(range(N), w2)
        )
        for right_combo, v_r in right_iter:
            syn_r = 0
            for p in right_combo:
                syn_r ^= col_ints[p]
            if syn_r not in left:
                continue
            right_set = set(right_combo)
            for left_combo, v_l in left[syn_r]:
                if any(p in right_set for p in left_combo):
                    continue  # positions must be disjoint
                if _is_logical(v_l ^ v_r, rref, pivot_map):
                    return weight

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute Lee distance of a qiskit-qec codebase entry."
    )
    parser.add_argument("n", type=int, help="Number of physical qubits")
    parser.add_argument("k", type=int, help="Number of logical qubits")
    parser.add_argument("id", type=int, help="Code ID (index in the JSON database)")
    parser.add_argument(
        "--max-weight", type=int, default=10,
        help="Maximum Lee weight to search (default: 10)"
    )
    args = parser.parse_args()

    code = load_code(args.n, args.k, args.id)
    print(f"[[{args.n},{args.k}]] code, ID={args.id}")
    print(f"  Pauli distance (stored): {code.get('d', '?')}")
    print(f"  Generators: {code['isotropic_generators']}")

    H_rows = generators_to_H(code["isotropic_generators"], args.n)
    d_lee = lee_distance(H_rows, args.n, args.max_weight)

    if d_lee == 0:
        print(f"  Lee distance: > {args.max_weight}")
    else:
        print(f"  Lee distance: {d_lee}")


if __name__ == "__main__":
    main()
