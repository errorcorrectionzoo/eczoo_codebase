#!/usr/bin/env python3
"""Print code IDs whose physical-gate blocks have only detectable Paulis.

For each result entry with d > 1, k > 0, a Physical gate, and a Logical action,
this script loads the matching code from the qiskit-qec database.  It then checks
each block of the physical action, for example the block {0, 2} in CS†0-2 or the
block {2, 3, 6} in CCZ2-3-6.

By default, a block passes when every non-identity Pauli supported inside that
block is detectable.  Use --exact-block-support to only check Paulis whose
support is exactly the full block.

Example:
    python3 scripts/check_gate_block_detectability.py \
        results/alg6/css_diagonal_transversal_hier3_nlteq7.txt
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path
from typing import Iterator, Sequence

from circuit_level_distance import Pauli, is_detectable, parse_pauli, pauli_to_label

CODE_RE = re.compile(r"^Code \[\[(\d+),(\d+),(\d+)\]\], database id=(\d+)")
GROUP_RE = re.compile(r"^\[\[(\d+),(\d+),(\d+)\]\]$")
GROUP_ENTRY_RE = re.compile(
    r"^\s*id=(\d+)\s+\(([^)]+)\)\s+physical=(\S+)\s+logical=(\S+)"
)
PHYSICAL_RE = re.compile(r"^Physical gate:\s*(\S+)")
LOGICAL_RE = re.compile(r"^Logical action:\s*(\S+)")
TOKEN_RE = re.compile(r"(CCZ|CS†|CS|CZ|T†|T|S†|S|Z)\[?(\d+(?:-\d+)*)\]?")

DATA_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "qiskit_qec"
    / "codes"
    / "codebase"
    / "data"
    / "base"
    / "base_data"
)


def parse_entries(path: Path) -> Iterator[dict[str, object]]:
    current: dict[str, object] | None = None
    group: tuple[int, int, int] | None = None
    for line in path.read_text().splitlines():
        group_match = GROUP_RE.match(line)
        if group_match:
            if current is not None:
                yield current
                current = None
            group = tuple(map(int, group_match.groups()))
            continue

        group_entry_match = GROUP_ENTRY_RE.match(line)
        if group_entry_match and group is not None:
            code_id = int(group_entry_match.group(1))
            n, k, d = group
            yield {
                "n": n,
                "k": k,
                "d": d,
                "id": code_id,
                "physical": group_entry_match.group(3),
                "logical": group_entry_match.group(4),
            }
            continue

        match = CODE_RE.match(line)
        if match:
            if current is not None:
                yield current
            n, k, d, code_id = map(int, match.groups())
            current = {
                "n": n,
                "k": k,
                "d": d,
                "id": code_id,
                "physical": None,
                "logical": None,
            }
            continue

        if current is None:
            continue

        match = PHYSICAL_RE.match(line)
        if match:
            current["physical"] = match.group(1)
            continue

        match = LOGICAL_RE.match(line)
        if match:
            current["logical"] = match.group(1)

    if current is not None:
        yield current


def physical_blocks(physical: str) -> list[tuple[int, ...]]:
    """Return physical gate blocks from compact notation like T1T3CS†0-2."""
    if physical == "I":
        return []

    blocks: list[tuple[int, ...]] = []
    position = 0
    while position < len(physical):
        match = TOKEN_RE.match(physical, position)
        if match is None:
            raise ValueError(f"Could not parse physical gate near {physical[position:]!r}")
        qubits = tuple(int(item) for item in match.group(2).split("-"))
        blocks.append(qubits)
        position = match.end()
    return blocks


def load_code(n: int, k: int, d: int, code_id: int) -> dict:
    json_path = DATA_ROOT / f"n_{n}" / f"codes_n_{n}_k_{k}_d_{d}.json"
    with json_path.open() as handle:
        data = json.load(handle)
    return data[str(code_id)]


def paulis_on_block(
    block: Sequence[int], exact_block_support: bool = False
) -> Iterator[Pauli]:
    """Yield non-identity Paulis supported inside, or exactly on, a block."""
    positions = tuple(block)
    min_weight = len(positions) if exact_block_support else 1
    for weight in range(min_weight, len(positions) + 1):
        for support in itertools.combinations(positions, weight):
            for letters in itertools.product("XYZ", repeat=weight):
                x_bits = 0
                z_bits = 0
                for qubit, letter in zip(support, letters):
                    if letter in ("X", "Y"):
                        x_bits |= 1 << qubit
                    if letter in ("Z", "Y"):
                        z_bits |= 1 << qubit
                yield x_bits, z_bits


def block_failures(
    block: Sequence[int],
    stabilizers: Sequence[Pauli],
    exact_block_support: bool = False,
) -> list[Pauli]:
    return [
        pauli
        for pauli in paulis_on_block(block, exact_block_support=exact_block_support)
        if not is_detectable(pauli, stabilizers)
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results_file",
        type=Path,
        help="Algorithm 6 results file containing Code, Physical gate, and Logical action lines.",
    )
    parser.add_argument(
        "--exact-block-support",
        action="store_true",
        help="Only check Paulis supported exactly on every qubit of each block.",
    )
    parser.add_argument(
        "--show-failures",
        action="store_true",
        help="Print the first few undetectable block Paulis for codes that fail.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    entries = [
        entry
        for entry in parse_entries(args.results_file)
        if entry["d"] > 1
        and entry["k"] > 0
        and entry["physical"] is not None
        and entry["logical"] is not None
    ]

    passing: list[dict[str, object]] = []
    failing: list[tuple[dict[str, object], list[tuple[tuple[int, ...], list[Pauli]]]]] = []

    for entry in entries:
        code = load_code(
            int(entry["n"]), int(entry["k"]), int(entry["d"]), int(entry["id"])
        )
        stabilizers = [
            parse_pauli(label, int(entry["n"]))
            for label in code["isotropic_generators"]
        ]
        failures_by_block: list[tuple[tuple[int, ...], list[Pauli]]] = []

        for block in sorted(set(physical_blocks(str(entry["physical"])))):
            failures = block_failures(
                block,
                stabilizers,
                exact_block_support=args.exact_block_support,
            )
            if failures:
                failures_by_block.append((block, failures))

        if failures_by_block:
            failing.append((entry, failures_by_block))
        else:
            passing.append(entry)
            print(
                f"[[{entry['n']},{entry['k']},{entry['d']}]] "
                f"id={entry['id']} physical={entry['physical']}"
            )

    print()
    print(f"Checked entries : {len(entries)}")
    print(f"Passing entries : {len(passing)}")
    print(f"Failing entries : {len(failing)}")

    if args.show_failures and failing:
        print()
        print("Failures:")
        for entry, failures_by_block in failing:
            print(
                f"[[{entry['n']},{entry['k']},{entry['d']}]] "
                f"id={entry['id']} physical={entry['physical']}"
            )
            for block, failures in failures_by_block:
                labels = ", ".join(
                    pauli_to_label(pauli, int(entry["n"])) for pauli in failures[:8]
                )
                suffix = "" if len(failures) <= 8 else f", ... ({len(failures)} total)"
                print(f"  block={block}: {labels}{suffix}")


if __name__ == "__main__":
    main()
