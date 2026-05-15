#!/usr/bin/env python3

"""Compute stabilizer and coset weight enumerators for a codebase entry."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple, cast

import numpy as np


DEFAULT_DATA_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "qiskit_qec"
    / "codes"
    / "codebase"
    / "data"
    / "base"
    / "base_data"
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Calculate the stabilizer weight enumerator and the coset weight enumerators "
            "of S in N(S) for a codebase entry identified by n, k, and index."
        )
    )
    parser.add_argument("n", type=int, help="Number of physical qubits")
    parser.add_argument("k", type=int, help="Number of encoded qubits")
    parser.add_argument("index", type=int, help="Code index within the [[n,k]] family")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"Path to the codebase data root (default: {DEFAULT_DATA_ROOT})",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format",
    )
    parser.add_argument(
        "--list-cosets",
        action="store_true",
        help="Include every coset label instead of only grouped multiplicities",
    )
    parser.add_argument(
        "--skip-stored-check",
        action="store_true",
        help="Do not compare the computed stabilizer weight enumerator against the stored one",
    )
    return parser.parse_args()


def open_text(path: Path):
    """Open a text file that may be gzip-compressed."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def code_file_candidates(n: int, k: int, data_root: Path) -> List[Path]:
    """Return candidate data files for a given [[n,k]] family."""
    base = data_root / f"n_{n}" / f"codes_n_{n}_k_{k}.json"
    return [base, base.with_suffix(base.suffix + ".gz")]


def load_code_record(n: int, k: int, index: int, data_root: Path) -> Tuple[Path, Dict[str, object]]:
    """Load the requested code record from the single matching [[n,k]] data file."""
    for path in code_file_candidates(n, k, data_root):
        if not path.exists():
            continue
        with open_text(path) as handle:
            data = json.load(handle)
        entry = data.get(str(index))
        if entry is None:
            raise FileNotFoundError(f"Index {index} was not found in {path}")
        return path, entry

    raise FileNotFoundError(f"No data file found for [[{n},{k}]] under {data_root}")


def get_int(record: Dict[str, object], key: str) -> int:
    """Fetch an integer-valued field from a JSON-loaded record."""
    value = record[key]
    if not isinstance(value, int):
        raise TypeError(f"Expected integer field {key!r}, got {type(value).__name__}")
    return value


def get_str(record: Dict[str, object], key: str) -> str:
    """Fetch a string-valued field from a JSON-loaded record."""
    value = record[key]
    if not isinstance(value, str):
        raise TypeError(f"Expected string field {key!r}, got {type(value).__name__}")
    return value


def get_str_list(record: Dict[str, object], key: str) -> List[str]:
    """Fetch a list of strings from a JSON-loaded record."""
    value = record[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"Expected list[str] field {key!r}")
    return cast(List[str], value)


def parse_sparse_pauli(label: str, num_qubits: int) -> np.ndarray:
    """Convert a sparse Pauli label like X0Y3Z5 into a symplectic binary vector."""
    x = np.zeros(num_qubits, dtype=np.uint8)
    z = np.zeros(num_qubits, dtype=np.uint8)

    index = 0
    while index < len(label):
        pauli = label[index]
        index += 1
        start = index
        while index < len(label) and label[index].isdigit():
            index += 1
        qubit = int(label[start:index])
        if pauli in ("X", "Y"):
            x[qubit] = 1
        if pauli in ("Z", "Y"):
            z[qubit] = 1

    return np.concatenate((x, z))


def rref_gf2(matrix: np.ndarray) -> Tuple[np.ndarray, List[int]]:
    """Return the GF(2) reduced row echelon form and pivot columns."""
    reduced = np.array(matrix, dtype=np.uint8, copy=True) % 2
    if reduced.ndim != 2:
        reduced = np.atleast_2d(reduced)

    num_rows, num_cols = reduced.shape
    pivot_cols: List[int] = []
    pivot_row = 0

    for col in range(num_cols):
        found = None
        for row in range(pivot_row, num_rows):
            if reduced[row, col]:
                found = row
                break
        if found is None:
            continue

        if found != pivot_row:
            reduced[[pivot_row, found]] = reduced[[found, pivot_row]]

        for row in range(num_rows):
            if row != pivot_row and reduced[row, col]:
                reduced[row] ^= reduced[pivot_row]

        pivot_cols.append(col)
        pivot_row += 1
        if pivot_row == num_rows:
            break

    nonzero = np.where(reduced.any(axis=1))[0]
    if len(nonzero) == 0:
        return reduced[:0], pivot_cols
    return reduced[: nonzero[-1] + 1], pivot_cols


def row_basis(matrix: np.ndarray) -> np.ndarray:
    """Return a full-rank row basis over GF(2)."""
    return rref_gf2(matrix)[0]


def in_rowspace(vector: np.ndarray, basis: np.ndarray) -> bool:
    """Check whether a vector lies in the GF(2) row space of a basis."""
    if basis.size == 0:
        return not np.any(vector)
    return row_basis(np.vstack((basis, vector))).shape[0] == basis.shape[0]


def quotient_basis(stabilizer_basis: np.ndarray, logical_ops: np.ndarray) -> np.ndarray:
    """Extract an independent quotient basis from the stored logical operators."""
    basis_rows: List[np.ndarray] = []
    current = stabilizer_basis.copy()

    for vector in logical_ops:
        if not in_rowspace(vector, current):
            basis_rows.append(vector)
            current = row_basis(np.vstack((current, vector)))

    if basis_rows:
        return np.array(basis_rows, dtype=np.uint8)
    return np.zeros((0, stabilizer_basis.shape[1]), dtype=np.uint8)


def enumerate_span(basis: np.ndarray) -> np.ndarray:
    """Enumerate all elements of the span of a GF(2) basis."""
    if basis.size == 0:
        return np.zeros((1, 0), dtype=np.uint8)

    elements = np.zeros((1, basis.shape[1]), dtype=np.uint8)
    for vector in basis:
        doubled = elements ^ vector
        elements = np.vstack((elements, doubled))
    return elements


def symplectic_weights(elements: np.ndarray, num_qubits: int) -> np.ndarray:
    """Compute Pauli weights for an array of symplectic vectors."""
    x_part = elements[:, :num_qubits]
    z_part = elements[:, num_qubits:]
    return np.count_nonzero(np.bitwise_or(x_part, z_part), axis=1)


def coset_weight_enumerators(
    stabilizer_basis: np.ndarray, quotient_basis_vectors: np.ndarray, num_qubits: int
) -> Tuple[np.ndarray, Dict[str, Tuple[int, ...]], Dict[Tuple[int, ...], List[str]]]:
    """Compute the stabilizer and all quotient coset weight enumerators."""
    stabilizer_elements = enumerate_span(stabilizer_basis)
    stabilizer_enumerator = np.bincount(
        symplectic_weights(stabilizer_elements, num_qubits), minlength=num_qubits + 1
    )

    if quotient_basis_vectors.size == 0:
        labels = [""]
        reps = np.zeros((1, stabilizer_basis.shape[1]), dtype=np.uint8)
    else:
        reps = enumerate_span(quotient_basis_vectors)
        labels = [
            format(index, f"0{quotient_basis_vectors.shape[0]}b")
            for index in range(reps.shape[0])
        ]

    per_coset: Dict[str, Tuple[int, ...]] = {}
    grouped: Dict[Tuple[int, ...], List[str]] = defaultdict(list)

    for label, rep in zip(labels, reps):
        coset_elements = stabilizer_elements ^ rep
        enumerator = tuple(
            np.bincount(symplectic_weights(coset_elements, num_qubits), minlength=num_qubits + 1)
            .astype(int)
            .tolist()
        )
        per_coset[label] = enumerator
        grouped[enumerator].append(label)

    return stabilizer_enumerator.astype(int), per_coset, grouped


def basis_to_sparse_strings(basis: np.ndarray, num_qubits: int) -> List[str]:
    """Convert a symplectic basis to sparse Pauli strings."""
    labels: List[str] = []
    for vector in basis:
        pieces: List[str] = []
        x_part = vector[:num_qubits]
        z_part = vector[num_qubits:]
        for qubit, (x_bit, z_bit) in enumerate(zip(x_part, z_part)):
            if not x_bit and not z_bit:
                continue
            if x_bit and z_bit:
                pauli = "Y"
            elif x_bit:
                pauli = "X"
            else:
                pauli = "Z"
            pieces.append(f"{pauli}{qubit}")
        labels.append("".join(pieces) if pieces else "I")
    return labels


def build_result(
    record: Dict[str, object],
    source_file: Path,
    stabilizer_basis: np.ndarray,
    quotient_basis_vectors: np.ndarray,
    stabilizer_enumerator: np.ndarray,
    per_coset: Dict[str, Tuple[int, ...]],
    grouped: Dict[Tuple[int, ...], List[str]],
) -> Dict[str, Any]:
    """Build the structured result payload."""
    grouped_items = []
    for enumerator, labels in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        grouped_items.append(
            {
                "enumerator": list(enumerator),
                "multiplicity": len(labels),
                "labels": labels,
            }
        )

    result: Dict[str, Any] = {
        "uuid": get_str(record, "uuid"),
        "n": get_int(record, "n"),
        "k": get_int(record, "k"),
        "index": get_int(record, "index"),
        "code_type": get_str(record, "code_type"),
        "source_file": str(source_file),
        "stabilizer_rank": int(stabilizer_basis.shape[0]),
        "quotient_dimension": int(quotient_basis_vectors.shape[0]),
        "stabilizer_size": int(1 << stabilizer_basis.shape[0]),
        "num_cosets": int(len(per_coset)),
        "stabilizer_weight_enumerator": stabilizer_enumerator.astype(int).tolist(),
        "stored_weight_enumerator": record.get("weight_enumerator"),
        "quotient_basis": basis_to_sparse_strings(quotient_basis_vectors, get_int(record, "n")),
        "distinct_coset_weight_enumerators": grouped_items,
    }
    return result


def print_text(result: Dict[str, Any], list_cosets: bool, per_coset: Dict[str, Tuple[int, ...]]) -> None:
    """Print a readable text summary."""
    print(f"UUID: {result['uuid']}")
    print(
        f"Code: [[{result['n']},{result['k']}]] index {result['index']} "
        f"({result['code_type']})"
    )
    print(f"Source: {result['source_file']}")
    print(f"rank(S): {result['stabilizer_rank']}")
    print(f"dim(N(S)/S): {result['quotient_dimension']}")
    print(f"|S|: {result['stabilizer_size']}")
    print(f"|N(S)/S|: {result['num_cosets']}")
    print(f"WE(S): {result['stabilizer_weight_enumerator']}")
    if result["stored_weight_enumerator"] is not None:
        print(f"Stored WE(S): {result['stored_weight_enumerator']}")
    print("Quotient basis:")
    for index, label in enumerate(result["quotient_basis"]):
        print(f"  b{index}: {label}")
    print("Distinct coset weight enumerators:")
    for item in result["distinct_coset_weight_enumerators"]:
        print(f"  multiplicity={item['multiplicity']}: {item['enumerator']}")
        print("  labels=" + " ".join(item["labels"]))

    if list_cosets:
        print("All cosets:")
        for label, enumerator in per_coset.items():
            display_label = label if label else "0"
            print(f"  {display_label}: {list(enumerator)}")


def main() -> int:
    """Run the command-line entry point."""
    args = parse_args()

    try:
        source_file, record = load_code_record(args.n, args.k, args.index, args.data_root)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 1

    num_qubits = get_int(record, "n")
    stabilizer_input = np.array(
        [parse_sparse_pauli(label, num_qubits) for label in get_str_list(record, "isotropic_generators")],
        dtype=np.uint8,
    )
    logical_input = np.array(
        [parse_sparse_pauli(label, num_qubits) for label in get_str_list(record, "logical_ops")],
        dtype=np.uint8,
    )

    stabilizer_basis = row_basis(stabilizer_input)
    quotient_basis_vectors = quotient_basis(stabilizer_basis, logical_input)

    stabilizer_enumerator, per_coset, grouped = coset_weight_enumerators(
        stabilizer_basis, quotient_basis_vectors, num_qubits
    )

    if not args.skip_stored_check:
        stored = record.get("weight_enumerator")
        computed = stabilizer_enumerator.astype(int).tolist()
        if stored is not None and computed != stored:
            print(
                "Computed stabilizer weight enumerator does not match the stored entry.",
                file=sys.stderr,
            )
            print(f"stored={stored}", file=sys.stderr)
            print(f"computed={computed}", file=sys.stderr)
            return 2

    result = build_result(
        record,
        source_file,
        stabilizer_basis,
        quotient_basis_vectors,
        stabilizer_enumerator,
        per_coset,
        grouped,
    )

    if args.output == "json":
        payload = dict(result)
        if args.list_cosets:
            payload["cosets"] = {label if label else "0": list(values) for label, values in per_coset.items()}
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print_text(result, args.list_cosets, per_coset)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())