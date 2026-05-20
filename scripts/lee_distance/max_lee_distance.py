#!/usr/bin/env python3
"""Find the LC-equivalent of a qiskit-qec code with the highest Lee distance.

Usage: python max_lee_distance.py <n> <k> <id>

Enumerates all 3^n LC-equivalents under {I, S, SH} per qubit. H gates are
excluded because H only swaps X<->Z without changing Lee weight of any Pauli,
so H never affects the Lee distance of a code.

The Lee distance is bounded between d_Pauli and 2*d_Pauli.
"""

import argparse
import sys
import time
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lee_distance import load_code, generators_to_H, lee_distance

# Per-qubit operations (excluding H, which never changes Lee distance):
#   0 = I  : (x, z) -> (x, z)        X->X, Y->Y, Z->Z
#   1 = S  : (x, z) -> (x, x^z)      X->Y(+1), Y->X(-1), Z->Z(0)
#   2 = SH : (x, z) -> (z, x^z)      X->Z(0),  Y->X(-1), Z->Y(+1)
_OP_NAMES = ("I", "S", "SH")

_PAULI_LABEL = {(0, 0): "I", (1, 0): "X", (0, 1): "Z", (1, 1): "Y"}


def apply_lc(H_rows: list[int], n: int, ops: tuple[int, ...]) -> list[int]:
    """Apply per-qubit LC operations to H (rows as 2n-bit integers).

    Bit layout: bit q = x_q for q < n, bit n+q = z_q.
    """
    result = list(H_rows)
    for q, op in enumerate(ops):
        if op == 0:
            continue
        mask_x = 1 << q
        mask_z = 1 << (n + q)
        clear = ~(mask_x | mask_z)
        new_rows = []
        for row in result:
            xi = (row >> q) & 1
            zi = (row >> (n + q)) & 1
            row &= clear
            if op == 1:  # S: z_q ^= x_q
                row |= (xi << q) | ((xi ^ zi) << (n + q))
            else:         # SH: x_q = z_q, z_q = x_q ^ z_q
                row |= (zi << q) | ((xi ^ zi) << (n + q))
            new_rows.append(row)
        result = new_rows
    return result


def H_to_generators(H_rows: list[int], n: int) -> list[str]:
    """Convert integer rows back to compact Pauli strings (e.g. 'X0Z3Y5')."""
    gens = []
    for row in H_rows:
        parts = []
        for q in range(n):
            xi = (row >> q) & 1
            zi = (row >> (n + q)) & 1
            label = _PAULI_LABEL[(xi, zi)]
            if label != "I":
                parts.append(f"{label}{q}")
        gens.append("".join(parts) if parts else "I0")
    return gens


def find_max_lee_distance(
    H_rows: list[int], n: int, d_pauli: int
) -> tuple[tuple, int, list[int]]:
    """Brute-force search over all 3^n LC-equivalents.

    Returns (best_ops, best_dist, best_H_rows).

    Strategy: filter + refine.
    - Filter: run lee_distance(..., max_weight=best_dist). If a logical is
      found (non-zero return), this code cannot improve on best_dist; skip.
    - Refine: only for codes that pass the filter, find the exact Lee distance
      by searching up to the upper bound 2*d_pauli.

    Because most codes have Lee dist = d_pauli (the minimum), the filter
    terminates almost immediately for them once best_dist = d_pauli.
    """
    upper = 2 * d_pauli
    n_ops = 3 ** n

    # Initialise with the stored (identity) form
    d_init = lee_distance(H_rows, n, max_weight=upper)
    best_dist = d_init
    best_ops: tuple = (0,) * n
    best_H = list(H_rows)

    if best_dist >= upper:
        return best_ops, best_dist, best_H

    t0 = time.perf_counter()
    checked = 1  # already did identity

    for ops in product(range(3), repeat=n):
        if ops == (0,) * n:
            continue  # identity already handled above

        H_lc = apply_lc(H_rows, n, ops)

        # Fast filter: any logical at weight <= best_dist?
        if lee_distance(H_lc, n, max_weight=best_dist) != 0:
            checked += 1
            continue  # Lee dist <= best_dist, cannot improve

        # This code has Lee dist > best_dist; find exact distance
        d = lee_distance(H_lc, n, max_weight=upper)
        checked += 1

        if d > best_dist:
            best_dist = d
            best_ops = ops
            best_H = H_lc
            elapsed = time.perf_counter() - t0
            print(
                f"  [{checked}/{n_ops}] New best Lee dist = {best_dist}"
                f"  (ops: {_ops_str(ops, n)})  +{elapsed:.2f}s",
                flush=True,
            )
            if best_dist >= upper:
                break  # hit the 2*d_Pauli upper bound

    return best_ops, best_dist, best_H


def _ops_str(ops: tuple, n: int) -> str:
    parts = [f"q{q}:{_OP_NAMES[op]}" for q, op in enumerate(ops) if op != 0]
    return ", ".join(parts) if parts else "identity"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the LC-equivalent of a qiskit-qec code with the highest Lee distance."
    )
    parser.add_argument("n", type=int, help="Number of physical qubits")
    parser.add_argument("k", type=int, help="Number of logical qubits")
    parser.add_argument("id", type=int, help="Code ID (index in the JSON database)")
    args = parser.parse_args()

    n, k, code_id = args.n, args.k, args.id
    code = load_code(n, k, code_id)
    d_pauli = code["d"]
    upper = 2 * d_pauli

    print(f"[[{n},{k}]] code, ID={code_id}")
    print(f"  Stored generators : {code['isotropic_generators']}")
    print(f"  Pauli distance    : {d_pauli}")
    print(f"  Search space      : 3^{n} = {3**n} LC-equivalents")
    print(f"  Lee dist bounds   : {d_pauli} .. {upper}")
    print()

    H_rows = generators_to_H(code["isotropic_generators"], n)
    d_stored = lee_distance(H_rows, n, max_weight=upper)
    print(f"  Lee dist (stored form) = {d_stored}")
    print()

    t_start = time.perf_counter()
    best_ops, best_dist, best_H = find_max_lee_distance(H_rows, n, d_pauli)
    elapsed = time.perf_counter() - t_start

    print()
    print(f"=== Result ===")
    print(f"  Best Lee distance : {best_dist}  (upper bound: {upper})")
    print(f"  LC operations     : {_ops_str(best_ops, n)}")
    print(f"  Generators of best LC-equivalent:")
    for gen in H_to_generators(best_H, n):
        print(f"    {gen}")
    print(f"  Total time        : {elapsed:.2f}s")


if __name__ == "__main__":
    main()
