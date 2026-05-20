#!/usr/bin/env python3
"""Run Webster Algorithm 6 (Depth-One Logical Operators) on a CSS code from the eczoo database.

Algorithm 6 searches for a transversal (depth-one) implementation of a diagonal logical
Clifford-hierarchy operator at level t for a CSS stabilizer code. It uses the embedded-code
method from Webster, Brown, Bartlett (2022) / Webster thesis (2023), Section 3.4.4.

Usage:
    python run_alg6.py <n> <k> <code_id> [<t>]

Arguments:
    n        : number of physical qubits
    k        : number of logical qubits
    code_id  : integer index of the code in the eczoo database
    t        : Clifford hierarchy level to search (default: 2)
               t=2 searches for CZ/S-type operators, t=3 for CCZ/T-type, etc.

The code must be a CSS code (is_css == 1 in the database).

Example:
    python run_alg6.py 8 3 0 2

Output format (compact gate notation):
    Single-qubit gates: {gate}{qubit}  e.g. S4, S†3, Z1, T5
    Two-qubit gates:    {gate}{q1}{q2} e.g. CZ01
    Three-qubit gates:  {gate}{q1}{q2}{q3} e.g. CCZ012
    Gate names follow CSSLO convention: S=S, S†=S†, T=T, T†=T†, Z=Z, CZ=CZ, CCZ=CCZ
"""

import sys
import io
import json
import re
import glob
import contextlib
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "src/qiskit_qec/codes/codebase/data/base/base_data"
CSSLO_DIR = Path(__file__).parent.parent.parent / "CSSLO"

sys.path.insert(0, str(CSSLO_DIR))
from CSSLO import depth_one_t, CSSCode

_PAULI_RE = re.compile(r"([XYZI])(\d+)")
_NPINT_RE = re.compile(r"np\.int64\((\d+)\)")

# Maps CSSLO gate-name tokens to display symbols.
# CSSLO encodes S^p as "S{p}" (omitting p=1), Z^p as "Z{p}", etc.
_GATE_DISPLAY = {
    # Single-qubit diagonal gates (powers of T = e^{iπ/4} rotation)
    "Z": "Z", "Z2": "I",
    "S": "S",   "S3": "S†",
    "T": "T",   "T7": "T†",
    # Two-qubit controlled gates (one C prefix in CSSLO)
    "CZ": "CZ",
    "CS": "CS",  "CS3": "CS†",
    "CT": "CT",  "CT7": "CT†",
    # Three-qubit doubly-controlled gates (two C prefixes)
    "CCZ": "CCZ",
    "CCS": "CCS", "CCS3": "CCS†",
    "CCT": "CCT", "CCT7": "CCT†",
}

# Regex to match one gate token: gate-name then one-or-more [qubit,...] bracket groups
_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)((?:\[[^\]]+\])+)")


def clean_csslo(s: str) -> str:
    """Remove np.int64(...) wrappers from a CSSLO output string."""
    return _NPINT_RE.sub(r"\1", s)


def format_op(csslo_str: str) -> str:
    """Convert a CSSLO gate string to compact notation.

    CSSLO format:  S3[2][3] S[4][5][6][7] CZ[0,1]
    Output format: S†2 S†3 S4 S5 S6 S7 CZ[0,1]

    Single-qubit gates: {gate}{qubit}  — no brackets, safe because gate names
    contain no digits, so e.g. S†12 unambiguously means S† on qubit 12.
    Multi-qubit gates:  {gate}[q1,q2,...] — brackets required for scalability.
    """
    s = clean_csslo(csslo_str).strip()
    # Strip leading phase factor (e.g. "w1/2 ...")
    if s.startswith("w"):
        space = s.find(" ")
        s = s[space:].strip() if space != -1 else ""
    if not s or s == "I":
        return "I"
    parts = []
    for m in _TOKEN_RE.finditer(s):
        gate_name = m.group(1)
        display = _GATE_DISPLAY.get(gate_name, gate_name)
        for bracket in re.findall(r"\[([^\]]+)\]", m.group(2)):
            qubits = [q.strip() for q in bracket.split(",")]
            if len(qubits) == 1:
                parts.append(f"{display}{qubits[0]}")
            else:
                parts.append(f"{display}{'-'.join(qubits)}")
    return "".join(parts) if parts else "I"


def load_code(n: int, k: int, code_id: int) -> tuple:
    """Search all d-files for [[n,k]] codes and return the entry with the given index."""
    pattern = str(DATA_DIR / f"n_{n}" / f"codes_n_{n}_k_{k}_d_*.json")
    key = str(code_id)
    for fpath in sorted(glob.glob(pattern)):
        data = json.load(open(fpath))
        if key in data:
            return data[key], fpath
    return None, None


def parse_generator(gen_str: str, n: int) -> tuple:
    """Parse 'X0X1Z2Z3' into (x_vec, z_vec) binary numpy arrays of length n."""
    x = np.zeros(n, dtype=int)
    z = np.zeros(n, dtype=int)
    for m in _PAULI_RE.finditer(gen_str):
        op, idx = m.group(1), int(m.group(2))
        if op in ("X", "Y"):
            x[idx] = 1
        if op in ("Z", "Y"):
            z[idx] = 1
    return x, z


def generators_to_SX_SZ(code: dict) -> tuple:
    """Extract binary S_X and S_Z matrices from a CSS code database entry.

    For CSS codes whose generators include Y Paulis (both x and z bits set on
    the same qubit), the Y components are converted to X by zeroing the z bit.
    This is valid for CSS codes: any Y-type generator can be reduced to a pure
    X-type generator by multiplying out the Z stabilizer component.
    """
    n = code["n"]
    is_css = code.get("is_css", 0)
    sx_rows, sz_rows = [], []
    converted = False
    for gen in code.get("isotropic_generators", []):
        x, z = parse_generator(gen, n)
        y_mask = x & z  # positions with Y (both bits set)
        if np.any(y_mask):
            if not is_css:
                return None, None
            # Y -> X: clear z bits where Y appears
            z = z & ~y_mask
            converted = True
        has_x, has_z = np.any(x), np.any(z)
        if has_x and not has_z:
            sx_rows.append(x)
        elif has_z and not has_x:
            sz_rows.append(z)
        else:
            # Still mixed after stripping — cannot classify
            return None, None
    if converted:
        pass  # suppress note in batch-friendly output
    SX = np.array(sx_rows, dtype=int) if sx_rows else np.zeros((0, n), dtype=int)
    SZ = np.array(sz_rows, dtype=int) if sz_rows else np.zeros((0, n), dtype=int)
    return SX, SZ


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    n = int(sys.argv[1])
    k = int(sys.argv[2])
    code_id = int(sys.argv[3])
    t = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    code, fpath = load_code(n, k, code_id)
    if code is None:
        sys.exit(
            f"Error: code n={n}, k={k}, id={code_id} not found in database.\n"
            f"Searched: {DATA_DIR / f'n_{n}' / f'codes_n_{n}_k_{k}_d_*.json'}"
        )

    d = code.get("d", "?")
    header = f"Code [[{n},{k},{d}]], database id={code_id}"
    print(header)

    is_css = code.get("is_css", 0)
    if not is_css:
        return

    SX, SZ = generators_to_SX_SZ(code)
    if SX is None:
        return

    SX_can, LX_can, SZ_can, LZ_can = CSSCode(SX, SZ=SZ)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = depth_one_t(SX_can, LX_can, t=t)
    csslo_out = buf.getvalue()

    if result is not None:
        _, target = result
        phys_raw = None
        for line in csslo_out.splitlines():
            if "Logical Operator:" in line:
                phys_raw = line.split(":", 1)[1].strip()
                break
        print(f"Physical gate:  {format_op(phys_raw) if phys_raw else '(not captured)'}")
        print(f"Logical action: {format_op(target)}")


if __name__ == "__main__":
    main()
