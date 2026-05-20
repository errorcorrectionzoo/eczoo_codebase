#!/usr/bin/env python3
"""Run Webster Algorithm 6 on a Pauli stabilizer code (CSS or non-CSS) from the eczoo database.

For non-CSS codes, the code is first converted to an equivalent CSS code via the
canonical XP algebra construction (Webster 2023, Section 3.4.4): any stabilizer code
C1 satisfies C1 = XP_2(0|q|0) * B * C2 where C2 is a CSS code, q is a binary vector
of single-qubit X gates, and B is a diagonal level-2 Clifford. Algorithm 6 is then
run on the underlying CSS code C2 to find diagonal transversal gates.

Usage:
    python run_alg6_stabilizer.py <n> <k> <code_id> [<t>]

Arguments:
    n        : number of physical qubits
    k        : number of logical qubits
    code_id  : integer index of the code in the eczoo database
    t        : Clifford hierarchy level to search (default: 2)
               t=2 searches for CZ/S-type operators, t=3 for CCZ/T-type, etc.

Output format (compact gate notation):
    Single-qubit gates: {gate}{qubit}  e.g. S4, S†3, Z1, T5
    Two-qubit gates:    {gate}[q1-q2]  e.g. CZ[0-1]
    Three-qubit gates:  {gate}[q1-q2-q3] e.g. CCZ[0-1-2]
"""

import sys
import io
import json
import re
import glob
import contextlib
import itertools
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "src/qiskit_qec/codes/codebase/data/base/base_data"
CSSLO_DIR = Path(__file__).parent.parent.parent / "CSSLO"

sys.path.insert(0, str(CSSLO_DIR))
from CSSLO import depth_one_t, CSSCode, action2CP
from XP_algebra import genMatrix, canonicalGenerators, XPSimplifyX, XPn, XPComponents, XPGenProd
from XCP_algebra import CP2Str
from common import ZMat, ZMatZeros, ZMat2str, argsort, set2Bin, leadingIndex
from NHow import getK

_PAULI_RE = re.compile(r"([XYZI])(\d+)")
_NPINT_RE = re.compile(r"np\.int64\((\d+)\)")

_GATE_DISPLAY = {
    "Z": "Z", "Z2": "I",
    "S": "S",   "S3": "S†",
    "T": "T",   "T7": "T†",
    "CZ": "CZ",
    "CS": "CS",  "CS3": "CS†",
    "CT": "CT",  "CT7": "CT†",
    "CCZ": "CCZ",
    "CCS": "CCS", "CCS3": "CCS†",
    "CCT": "CCT", "CCT7": "CCT†",
}

_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)((?:\[[^\]]+\])+)")


def clean_csslo(s: str) -> str:
    return _NPINT_RE.sub(r"\1", s)


def format_op(csslo_str: str) -> str:
    s = clean_csslo(csslo_str).strip()
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
                parts.append(f"{display}[{'-'.join(qubits)}]")
    return "".join(parts) if parts else "I"


def load_code(n: int, k: int, code_id: int) -> tuple:
    pattern = str(DATA_DIR / f"n_{n}" / f"codes_n_{n}_k_{k}_d_*.json")
    key = str(code_id)
    for fpath in sorted(glob.glob(pattern)):
        data = json.load(open(fpath))
        if key in data:
            return data[key], fpath
    return None, None


def parse_generator(gen_str: str, n: int) -> tuple:
    x = np.zeros(n, dtype=int)
    z = np.zeros(n, dtype=int)
    for m in _PAULI_RE.finditer(gen_str):
        op, idx = m.group(1), int(m.group(2))
        if op in ("X", "Y"):
            x[idx] = 1
        if op in ("Z", "Y"):
            z[idx] = 1
    return x, z


def generators_to_SX_SZ_SXZ(code: dict) -> tuple:
    """Parse stabilizer generators into SX, SZ, SXZ matrices (codetables.de convention).

    SX:  X components of generators that have at least one X (or Y) qubit.
    SZ:  Z components of pure-Z generators.
    SXZ: Z components of generators that have at least one X (or Y) qubit.
    """
    n = code["n"]
    sx_rows, sz_rows, sxz_rows = [], [], []
    for gen in code.get("isotropic_generators", []):
        x, z = parse_generator(gen, n)
        if np.any(x):
            sx_rows.append(x)
            sxz_rows.append(z)
        else:
            sz_rows.append(z)
    SX  = ZMat(sx_rows,  n) if sx_rows  else ZMatZeros((0, n))
    SZ  = ZMat(sz_rows,  n) if sz_rows  else ZMatZeros((0, n))
    SXZ = ZMat(sxz_rows, n) if sxz_rows else ZMatZeros((0, n))
    return SX, SZ, SXZ


def get_q(SZ_xp: np.ndarray) -> np.ndarray:
    """Find a binary vector q such that every Z-stabilizer fixes X^q|0>.

    SZ_xp rows are XP-format Z-type canonical generators (shape s × 2n+2).
    Returns an n-bit vector, or all-zeros if SZ is empty or no solution found.
    """
    s, ncols = np.shape(SZ_xp)
    n_q = ncols // 2 - 1
    if s == 0:
        return ZMatZeros(n_q)
    # In XP format the z-bits start at column n_q+1 and the last column carries
    # Howell-form remainder information (used as the phase constraint here).
    n = ncols // 2   # = n_q + 1
    pVec = SZ_xp[:, -1:]          # last column (Howell remainder / phase constraint)
    Sz   = SZ_xp[:, n:-1]         # z-bit columns
    A    = np.hstack([pVec, Sz])
    K    = getK(A, 2)
    if len(K) > 0 and K[0][0] == 1:
        return K[0, 1:]
    return ZMatZeros(n_q)


def get_D(SX_xp: np.ndarray, LX_xp: np.ndarray, n: int) -> tuple:
    """Find the diagonal CP operator D encoding codeword phases.

    Returns (qVec, V) for a CP^V_4(qVec) operator such that
    D = CP2Str(2*qVec, V, 4) gives the phase correction operator.
    """
    GX   = np.vstack([SX_xp, LX_xp])
    gLen = len(GX)
    V, pVec = [], []
    for r in range(gLen + 1):
        for combo in itertools.combinations(range(gLen), r=r):
            u = set2Bin(gLen, combo)
            a = XPGenProd(GX, u, 2)
            p, x, z = XPComponents(a)
            V.append(tuple(x))
            pVec.append(p)
    pVec = ZMat(pVec)
    V    = ZMat(V)
    # Mask to leading-index columns of GX (X part)
    LiX  = [leadingIndex(a[:n]) for a in GX]
    mask = set2Bin(n, LiX)
    V    = V * mask
    # Sort by weight then lexicographic order
    order = [(int(np.sum(v)), tuple(v)) for v in V]
    ix   = argsort(order)
    V, pVec = V[ix], pVec[ix]
    qVec = action2CP(V, pVec, 4)
    return qVec, V


def stabilizer_to_css(code: dict) -> tuple:
    """Convert any stabilizer code to its underlying CSS code.

    Returns:
        Sx     : binary X-check matrix of the underlying CSS code
        Lx     : binary X-logical matrix
        SX_xp  : canonical XP-format X-type stabilizer generators
        SZ_xp  : canonical XP-format Z-type stabilizer generators
        LX_xp  : canonical XP-format X-type logical operators
    """
    n = code["n"]
    SX_bin, SZ_bin, SXZ_bin = generators_to_SX_SZ_SXZ(code)
    G                       = genMatrix(SX_bin, SZ_bin, SXZ_bin, randomsign=False)
    SX_xp, SZ_xp, LX_xp, _ = canonicalGenerators(G)
    if len(LX_xp) > 0:
        LX_xp = XPSimplifyX(LX_xp)
    Sx = SX_xp[:, :n]
    Lx = LX_xp[:, :n]
    return Sx, Lx, SX_xp, SZ_xp, LX_xp


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    n       = int(sys.argv[1])
    k       = int(sys.argv[2])
    code_id = int(sys.argv[3])
    t       = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    code, _ = load_code(n, k, code_id)
    if code is None:
        sys.exit(
            f"Error: code n={n}, k={k}, id={code_id} not found in database.\n"
            f"Searched: {DATA_DIR / f'n_{n}' / f'codes_n_{n}_k_{k}_d_*.json'}"
        )

    d      = code.get("d", "?")
    is_css = code.get("is_css", 0)
    print(f"Code [[{n},{k},{d}]], database id={code_id}, {'CSS' if is_css else 'non-CSS'}")

    Sx, Lx, SX_xp, SZ_xp, LX_xp = stabilizer_to_css(code)

    if not is_css:
        q    = get_q(SZ_xp)
        print(f"q = {ZMat2str(q)}")
        qVec, V = get_D(SX_xp, LX_xp, n)
        print(f"D = {clean_csslo(CP2Str(2 * qVec, V, 4))}")

    SX_can, LX_can, _, _ = CSSCode(Sx, LX=Lx)

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
    else:
        print(f"No depth-one diagonal t={t} logical found.")


if __name__ == "__main__":
    main()
