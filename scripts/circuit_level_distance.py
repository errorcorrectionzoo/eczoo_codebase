#!/usr/bin/env python3
"""Compute circuit-level distance from Pauli-support conjugation.

The script enumerates all detectable Pauli strings of weight up to d - 1 for a
stabilizer code, conjugates each by a circuit, and checks whether every Pauli in
the resulting support is detectable.  It supports common Clifford gates plus the
diagonal non-Clifford gates T, powers of T, CS, CSdg, and CCZ.

Standalone demo:
    python3 scripts/circuit_level_distance.py

Example with explicit gates:
    python3 scripts/circuit_level_distance.py --code-id 18 \
        --gate S0 --gate S2 --gate SDG3 --gate CZ1,4
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import numpy as np

Pauli = tuple[int, int]  # (x_bits, z_bits), phase ignored
Gate = tuple[str, tuple[int, ...]]

_PAULI_RE = re.compile(r"([IXYZ])(\d+)")
_GATE_NAMES = (
    "CSDG",
    "CSDAG",
    "CCZ",
    "SWAP",
    "CNOT",
    "SDG",
    "TDG",
    "CS",
    "CX",
    "CY",
    "CZ",
    "T",
    "H",
    "S",
    "X",
    "Y",
    "Z",
    "I",
)

_I2 = np.eye(2, dtype=complex)
_X = np.array([[0, 1], [1, 0]], dtype=complex)
_Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
_Z = np.array([[1, 0], [0, -1]], dtype=complex)
_H = (1 / np.sqrt(2)) * np.array([[1, 1], [1, -1]], dtype=complex)
_S = np.diag([1, 1j]).astype(complex)
_T = np.diag([1, np.exp(1j * np.pi / 4)]).astype(complex)
_PAULI_MATS = {
    (0, 0): _I2,
    (1, 0): _X,
    (1, 1): _Y,
    (0, 1): _Z,
}
_LOCAL_LETTERS = {
    (0, 0): "I",
    (1, 0): "X",
    (1, 1): "Y",
    (0, 1): "Z",
}


def parse_pauli(label: str, n: int) -> Pauli:
    """Parse sparse labels such as ``X0Z1Y3`` into integer x/z bit masks."""
    x_bits = 0
    z_bits = 0
    compact = label.replace(" ", "")
    if compact in ("", "I"):
        return 0, 0

    seen: set[int] = set()
    for pauli, q_str in _PAULI_RE.findall(compact):
        q = int(q_str)
        if q < 0 or q >= n:
            raise ValueError(f"Qubit index {q} is outside n={n} in {label!r}")
        if q in seen:
            raise ValueError(f"Qubit {q} appears more than once in {label!r}")
        seen.add(q)
        if pauli in ("X", "Y"):
            x_bits |= 1 << q
        if pauli in ("Z", "Y"):
            z_bits |= 1 << q

    if not seen:
        raise ValueError(f"Invalid Pauli label: {label!r}")
    return x_bits, z_bits


def pauli_to_label(pauli: Pauli, n: int) -> str:
    """Convert integer x/z bit masks into sparse ``X0Y1Z3`` notation."""
    x_bits, z_bits = pauli
    pieces: list[str] = []
    for q in range(n):
        key = ((x_bits >> q) & 1, (z_bits >> q) & 1)
        letter = _LOCAL_LETTERS[key]
        if letter != "I":
            pieces.append(f"{letter}{q}")
    return "".join(pieces) or "I"


def pauli_weight(pauli: Pauli) -> int:
    x_bits, z_bits = pauli
    return (x_bits | z_bits).bit_count()


def anticommutes(a: Pauli, b: Pauli) -> bool:
    ax, az = a
    bx, bz = b
    return ((ax & bz).bit_count() + (az & bx).bit_count()) % 2 == 1


def is_detectable(pauli: Pauli, stabilizers: Sequence[Pauli]) -> bool:
    """A Pauli error is detectable here iff it anticommutes with a stabilizer."""
    return any(anticommutes(pauli, stab) for stab in stabilizers)


def all_paulis_up_to_weight(n: int, max_weight: int) -> Iterator[Pauli]:
    """Yield all non-identity n-qubit Paulis with weight at most max_weight."""
    for weight in range(1, max_weight + 1):
        for positions in itertools.combinations(range(n), weight):
            for letters in itertools.product(("X", "Y", "Z"), repeat=weight):
                x_bits = 0
                z_bits = 0
                for q, letter in zip(positions, letters):
                    if letter in ("X", "Y"):
                        x_bits |= 1 << q
                    if letter in ("Z", "Y"):
                        z_bits |= 1 << q
                yield x_bits, z_bits


def _kron_all(mats: Iterable[np.ndarray]) -> np.ndarray:
    out = np.array([[1]], dtype=complex)
    for mat in mats:
        out = np.kron(out, mat)
    return out


def _diag_gate(num_qubits: int, phase_for_bits: dict[tuple[int, ...], complex]) -> np.ndarray:
    dim = 1 << num_qubits
    diag = np.ones(dim, dtype=complex)
    for idx in range(dim):
        bits = tuple((idx >> (num_qubits - 1 - pos)) & 1 for pos in range(num_qubits))
        diag[idx] = phase_for_bits.get(bits, 1)
    return np.diag(diag)


def _gate_unitary(name: str, arity: int) -> np.ndarray:
    name = _canonical_gate_name(name)
    if re.fullmatch(r"T-?\d+", name):
        power = int(name[1:]) % 8
        return np.linalg.matrix_power(_T, power)

    one_qubit = {
        "I": _I2,
        "X": _X,
        "Y": _Y,
        "Z": _Z,
        "H": _H,
        "S": _S,
        "SDG": _S.conj().T,
        "T": _T,
        "TDG": _T.conj().T,
    }
    if arity == 1 and name in one_qubit:
        return one_qubit[name]

    if arity == 2:
        if name in ("CX", "CNOT"):
            return np.array(
                [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0]],
                dtype=complex,
            )
        if name == "CY":
            return np.array(
                [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, -1j], [0, 0, 1j, 0]],
                dtype=complex,
            )
        if name == "CZ":
            return _diag_gate(2, {(1, 1): -1})
        if name == "CS":
            return _diag_gate(2, {(1, 1): 1j})
        if name in ("CSDG", "CSDAG"):
            return _diag_gate(2, {(1, 1): -1j})
        if name == "SWAP":
            return np.array(
                [[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
                dtype=complex,
            )

    if arity == 3 and name == "CCZ":
        return _diag_gate(3, {(1, 1, 1): -1})

    raise ValueError(f"Unsupported gate {name!r} with {arity} qubits")


def _local_pauli_matrix(local: tuple[tuple[int, int], ...]) -> np.ndarray:
    return _kron_all(_PAULI_MATS[item] for item in local)


def _all_local_paulis(arity: int) -> Iterator[tuple[tuple[int, int], ...]]:
    yield from itertools.product(((0, 0), (1, 0), (1, 1), (0, 1)), repeat=arity)


@lru_cache(maxsize=None)
def _local_conjugation_support(
    name: str, local: tuple[tuple[int, int], ...]
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return exact Pauli support of U P Udag for one local gate."""
    table_support = _support_table_conjugation(name, local)
    if table_support is not None:
        return table_support

    arity = len(local)
    unitary = _gate_unitary(name, arity)
    matrix = unitary @ _local_pauli_matrix(local) @ unitary.conj().T
    dim = 1 << arity
    support: list[tuple[tuple[int, int], ...]] = []

    for candidate in _all_local_paulis(arity):
        basis = _local_pauli_matrix(candidate)
        coeff = np.trace(basis.conj().T @ matrix) / dim
        if abs(coeff) > 1e-9:
            support.append(candidate)

    return tuple(support)


def _multiply_local(
    a: tuple[tuple[int, int], ...], b: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, int], ...]:
    return tuple((ax ^ bx, az ^ bz) for (ax, az), (bx, bz) in zip(a, b))


def _single_local_support(
    arity: int, index: int, choices: Iterable[tuple[int, int]]
) -> tuple[tuple[tuple[int, int], ...], ...]:
    out = []
    for choice in choices:
        item = [(0, 0)] * arity
        item[index] = choice
        out.append(tuple(item))
    return tuple(out)


def _z_subset_support(
    arity: int, x_or_y_index: int, z_indices: Sequence[int]
) -> tuple[tuple[tuple[int, int], ...], ...]:
    out = []
    for xy in ((1, 0), (1, 1)):
        for include in itertools.product((False, True), repeat=len(z_indices)):
            item = [(0, 0)] * arity
            item[x_or_y_index] = xy
            for enabled, z_index in zip(include, z_indices):
                if enabled:
                    item[z_index] = (0, 1)
            out.append(tuple(item))
    return tuple(out)


def _combine_table_factors(
    arity: int, factors: Iterable[tuple[tuple[tuple[int, int], ...], ...]]
) -> tuple[tuple[tuple[int, int], ...], ...]:
    support = {tuple([(0, 0)] * arity)}
    for factor in factors:
        support = {
            _multiply_local(current, branch)
            for current in support
            for branch in factor
        }
    return tuple(sorted(support))


def _support_table_conjugation(
    name: str, local: tuple[tuple[int, int], ...]
) -> tuple[tuple[tuple[int, int], ...], ...] | None:
    """Apply the supplied Pauli-support tables for T/CS/CSdg/CCZ.

    For a multi-qubit Pauli on the same gate support, table entries for the
    non-identity one-qubit factors are multiplied with phases ignored.
    """
    name = _canonical_gate_name(name)
    arity = len(local)

    t_power_match = re.fullmatch(r"T(-?\d+)", name)
    if t_power_match:
        power = int(t_power_match.group(1)) % 8
    elif name == "T":
        power = 1
    elif name == "TDG":
        power = 7
    else:
        power = None

    if arity == 1 and power is not None and power % 2 == 1:
        if local[0] == (0, 0):
            return (local,)
        if local[0] == (0, 1):
            return (local,)
        return _single_local_support(1, 0, ((1, 0), (1, 1)))

    if arity == 2 and name in ("CS", "CSDG", "CSDAG"):
        factors = []
        for index, item in enumerate(local):
            if item == (0, 0):
                continue
            if item == (0, 1):
                factors.append(_single_local_support(2, index, ((0, 1),)))
            elif index == 0:
                factors.append(
                    (
                        ((1, 0), (0, 0)),
                        ((1, 0), (0, 1)),
                        ((1, 1), (0, 0)),
                        ((1, 1), (0, 1)),
                    )
                )
            else:
                factors.append(
                    (
                        ((0, 0), (1, 0)),
                        ((0, 0), (1, 1)),
                        ((0, 1), (1, 0)),
                        ((0, 1), (1, 1)),
                    )
                )
        return _combine_table_factors(2, factors)

    if arity == 3 and name == "CCZ":
        factors = []
        for index, item in enumerate(local):
            if item == (0, 0):
                continue
            if item == (0, 1):
                factors.append(_single_local_support(3, index, ((0, 1),)))
            else:
                others = [other for other in range(3) if other != index]
                factors.append(_z_subset_support(3, index, others))
        return _combine_table_factors(3, factors)

    return None


def conjugate_pauli_support(pauli: Pauli, gates: Sequence[Gate]) -> set[Pauli]:
    """Conjugate one Pauli by a circuit and return the resulting Pauli support."""
    support = {pauli}
    for name, qubits in gates:
        next_support: set[Pauli] = set()
        for term in support:
            x_bits, z_bits = term
            local = tuple(
                (((x_bits >> q) & 1), ((z_bits >> q) & 1)) for q in qubits
            )
            for local_out in _local_conjugation_support(name, local):
                out_x, out_z = x_bits, z_bits
                for q, (lx, lz) in zip(qubits, local_out):
                    mask = 1 << q
                    out_x = (out_x | mask) if lx else (out_x & ~mask)
                    out_z = (out_z | mask) if lz else (out_z & ~mask)
                next_support.add((out_x, out_z))
        support = next_support
    return support


def circuit_level_distance(
    n: int,
    d: int,
    stabilizer_labels: Sequence[str],
    gates: Sequence[Gate | tuple],
    verbose: bool = False,
) -> tuple[int, bool, list[dict[str, object]]]:
    """Compute circuit-level distance.

    A detectable Pauli ``P`` remains detectable after conjugation when every
    Pauli term in the support of ``C P Cdag`` is detectable.
    """
    if d <= 1:
        raise ValueError("Circuit-level distance is only meaningful here for d > 1")

    stabilizers = [parse_pauli(stab, n) for stab in stabilizer_labels]
    normalized_gates = [_normalize_gate(gate) for gate in gates]

    max_weight = 0
    rows: list[dict[str, object]] = []

    for pauli in all_paulis_up_to_weight(n, d - 1):
        if not is_detectable(pauli, stabilizers):
            continue

        image = conjugate_pauli_support(pauli, normalized_gates)
        detectable_terms = [term for term in image if is_detectable(term, stabilizers)]
        remains_detectable = len(detectable_terms) == len(image)
        weight = pauli_weight(pauli)

        if remains_detectable:
            max_weight = max(max_weight, weight)

        row = {
            "pauli": pauli,
            "weight": weight,
            "image": sorted(image),
            "remains_detectable": remains_detectable,
            "undetectable_image_terms": sorted(set(image) - set(detectable_terms)),
        }
        rows.append(row)

        if verbose:
            original = pauli_to_label(pauli, n)
            labels = ", ".join(pauli_to_label(term, n) for term in sorted(image))
            tag = "detectable" if remains_detectable else "UNDETECTABLE"
            print(f"  {original:12s} -> {{{labels}}} [{tag}]")

    distance = 1 + max_weight
    return distance, distance == d, rows


def _canonical_gate_name(name: str) -> str:
    upper = name.upper().replace("DAGGER", "DG")
    aliases = {
        "ID": "I",
        "PHASE": "S",
        "P": "S",
        "SDAG": "SDG",
        "S†": "SDG",
        "TDAG": "TDG",
        "T†": "TDG",
        "CSDAG": "CSDG",
        "CS†": "CSDG",
    }
    return aliases.get(upper, upper)


def _normalize_gate(gate: Gate | tuple) -> Gate:
    if len(gate) == 2 and isinstance(gate[1], tuple):
        name = str(gate[0])
        qubits = tuple(int(q) for q in gate[1])
    else:
        name = str(gate[0])
        qubits = tuple(int(q) for q in gate[1:])
    return _canonical_gate_name(name), qubits


def parse_gate(text: str) -> Gate:
    """Parse compact gate strings such as ``S0``, ``SDG3``, or ``CZ1,4``."""
    cleaned = text.strip().replace("{", "").replace("}", "")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.replace("†", "DG").replace("dagger", "DG").replace("Dagger", "DG")
    upper = cleaned.upper()

    power_match = re.match(r"^T\^(-?\d+)[_:,-]*(.*)$", upper)
    if power_match:
        name = f"T{int(power_match.group(1)) % 8}"
        qubit_text = power_match.group(2)
    else:
        name = ""
        qubit_text = ""
        for candidate in _GATE_NAMES:
            if upper.startswith(candidate):
                name = _canonical_gate_name(candidate)
                qubit_text = upper[len(candidate) :]
                break
        if not name:
            raise ValueError(f"Could not parse gate name in: {text!r}")

    qubit_text = qubit_text.lstrip("_:,-")
    qubits = tuple(int(item) for item in re.findall(r"\d+", qubit_text))
    if not qubits:
        raise ValueError(f"No qubits found in gate: {text!r}")

    return name, qubits


_DATA_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "qiskit_qec"
    / "codes"
    / "codebase"
    / "data"
    / "base"
    / "base_data"
)


def _default_json_path() -> Path:
    return _DATA_ROOT / "n_5" / "codes_n_5_k_1_d_2.json"


def _load_code(json_path: Path, code_id: str) -> dict:
    with json_path.open() as handle:
        codes = json.load(handle)
    try:
        return codes[str(code_id)]
    except KeyError as exc:
        raise KeyError(f"Code ID {code_id!r} was not found in {json_path}") from exc


def _format_circuit(gates: Sequence[Gate]) -> str:
    parts = []
    for name, qubits in gates:
        if len(qubits) == 1:
            parts.append(f"{name}_{qubits[0]}")
        else:
            parts.append(f"{name}_{{{','.join(str(q) for q in qubits)}}}")
    return " ".join(parts)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-json", type=Path, default=_default_json_path())
    parser.add_argument("--code-id", default="18")
    parser.add_argument(
        "--gate",
        action="append",
        default=None,
        help="Gate in compact form, e.g. S0, SDG3, T^3_2, CS0,1, CSDG0,1, CCZ0,1,2.",
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print each Pauli image.")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    code = _load_code(args.code_json, args.code_id)
    gates = (
        [parse_gate(item) for item in args.gate]
        if args.gate
        else [("S", (0,)), ("S", (2,)), ("SDG", (3,)), ("CZ", (1, 4))]
    )

    n = int(code["n"])
    k = int(code["k"])
    d = int(code["d"])
    stabilizers = list(code["isotropic_generators"])
    logical_ops = list(code.get("logical_ops", []))

    print(f"Code [[{n},{k},{d}]] (ID {args.code_id})")
    print(f"Stabilizers : {stabilizers}")
    print(f"Logical ops : {logical_ops}")
    print(f"Circuit     : {_format_circuit(gates)}")
    print()

    if not args.quiet:
        print(f"Detectable Paulis (weight 1..{d - 1}) and their images under C P Cdag:")
        print()

    distance, is_preserving, _ = circuit_level_distance(
        n, d, stabilizers, gates, verbose=not args.quiet
    )

    print()
    print(f"Circuit-level distance : {distance}")
    if is_preserving:
        print(f"Circuit is DISTANCE-PRESERVING (circuit-level distance = code distance = {d})")
    else:
        print(
            "Circuit is NOT distance-preserving "
            f"(circuit-level distance {distance} != code distance {d})"
        )


if __name__ == "__main__":
    main()
