#!/usr/bin/env python3
"""Determine the logical action of automorphism group generators for a code in the database.

For each generator in the aut_group_generators field, applies it (as a physical
Clifford operation) to the logical operator representatives and classifies the result
modulo the stabilizer group.  For k=1 the 6 possible symplectic actions are named;
for k>1 the symplectic matrix image is printed.

Gate notation used in the database:
    H   Hadamard       : X <-> Z
    S   Phase (sqrt Z) : X -> Y,  Z -> Z
    V   sqrt X = HSH   : X -> X,  Z -> Y
    R   HS             : X -> Y,  Z -> X
    r   SH             : X -> Z,  Z -> Y

Permutations use standard cycle notation, e.g. (1,4)(2,3).
Combined notation: "V0S4" means V on qubit 0 and S on qubit 4.
"H1H4^(1,4)" means H on qubits 1 and 4, then the transposition (1,4).

Usage:
    python aut_logical_action.py <n> <k> <id>

Example:
    python aut_logical_action.py 7 1 226
"""

import sys
import re
import json
import glob
from pathlib import Path

DATA_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "qiskit_qec" / "codes" / "codebase" / "data" / "base" / "base_data"
)

# ── symplectic gate matrices ──────────────────────────────────────────────────
# M acts on column (x, z): x' = M[0][0]*x + M[0][1]*z,  z' = M[1][0]*x + M[1][1]*z  (mod 2)
GATE_MAT = {
    'H': ((0, 1), (1, 0)),   # X <-> Z
    'S': ((1, 0), (1, 1)),   # X -> Y,  Z -> Z
    'V': ((1, 1), (0, 1)),   # X -> X,  Z -> Y  (= HSH)
    'R': ((1, 1), (1, 0)),   # X -> Y,  Z -> X  (= HS)
    'r': ((0, 1), (1, 1)),   # X -> Z,  Z -> Y  (= SH)
}

# ── helpers ───────────────────────────────────────────────────────────────────

def xor(a, b):
    return [(a[i] ^ b[i]) for i in range(len(a))]


def parse_pauli(s, n):
    """Parse a Pauli string like 'X0Z4Y2' into (x_vec, z_vec) of length n."""
    x = [0] * n
    z = [0] * n
    for m in re.finditer(r'([XYZI])(\d+)', s):
        op, q = m.group(1), int(m.group(2))
        if op in ('X', 'Y'):
            x[q] = 1
        if op in ('Z', 'Y'):
            z[q] = 1
    return x, z


def apply_gate_qubit(px, pz, q, mat):
    px, pz = list(px), list(pz)
    ox, oz = px[q], pz[q]
    px[q] = (mat[0][0] * ox + mat[0][1] * oz) % 2
    pz[q] = (mat[1][0] * ox + mat[1][1] * oz) % 2
    return px, pz


def apply_permutation(px, pz, cycles):
    """Apply qubit permutation given as list of cycles."""
    npx, npz = list(px), list(pz)
    for cyc in cycles:
        for i in range(len(cyc)):
            dst = cyc[(i + 1) % len(cyc)]
            npx[dst] = px[cyc[i]]
            npz[dst] = pz[cyc[i]]
    return npx, npz


def parse_cycles(s):
    """Extract cycle list from a string like '(1,4)(2,3)'."""
    return [[int(x) for x in c.split(',')] for c in re.findall(r'\(([^)]+)\)', s)]


def apply_generator(gen_str, px, pz):
    """Apply one automorphism generator string to a Pauli (px, pz)."""
    parts = gen_str.split('^', 1)
    sq_part = parts[0]
    perm_str = parts[1] if len(parts) > 1 else ''

    # Single-qubit gates: one letter followed by a qubit index
    for m in re.finditer(r'([HVSRr])(\d+)', sq_part):
        gate, q = m.group(1), int(m.group(2))
        px, pz = apply_gate_qubit(px, pz, q, GATE_MAT[gate])

    # Permutation from the ^ part, or from a bare cycle in the main part
    cycles = parse_cycles(perm_str) if perm_str else parse_cycles(sq_part)
    if cycles:
        px, pz = apply_permutation(px, pz, cycles)

    return px, pz


# ── stabilizer group membership ───────────────────────────────────────────────

def build_stabilizer_group(stab_symp):
    """Return set of all 2^m elements of the stabilizer group (symplectic tuples)."""
    m = len(stab_symp)
    n = len(stab_symp[0][0])
    group = set()
    for mask in range(1 << m):
        gx = [0] * n
        gz = [0] * n
        for i in range(m):
            if mask & (1 << i):
                gx = xor(gx, stab_symp[i][0])
                gz = xor(gz, stab_symp[i][1])
        group.add(tuple(gx + gz))
    return group


def in_coset(px, pz, target_x, target_z, stab_group):
    """Return True if (px,pz) is in the coset (target_x,target_z)·S."""
    diff_x = xor(px, target_x)
    diff_z = xor(pz, target_z)
    return tuple(diff_x + diff_z) in stab_group


# ── logical action classification (k=1) ──────────────────────────────────────

# Map (image_of_LX, image_of_LZ) to a named logical gate (symplectic class).
# LX, LZ, LY denote the three logical Pauli types.
K1_ACTION_NAMES = {
    ('LX', 'LZ'): 'Ī  (identity)',
    ('LZ', 'LX'): 'H̄  (Hadamard: X̄↔Z̄)',
    ('LY', 'LZ'): 'S̄  (phase: X̄→Ȳ, Z̄→Z̄)',
    ('LX', 'LY'): 'V̄  (√X̄ = H̄S̄H̄: X̄→X̄, Z̄→Ȳ)',
    ('LZ', 'LY'): 'S̄H̄ (X̄→Z̄, Z̄→Ȳ)',
    ('LY', 'LX'): 'H̄S̄ (X̄→Ȳ, Z̄→X̄)',
}


def classify_k1(img_lx, img_lz, LX, LZ, stab_group):
    """Classify the logical action for k=1."""
    n = len(LX[0])
    LY = (xor(LX[0], LZ[0]), xor(LX[1], LZ[1]))
    candidates = {'LX': LX, 'LZ': LZ, 'LY': LY, 'I': ([0]*n, [0]*n)}
    def classify_one(ipx, ipz):
        for name, (lx, lz) in candidates.items():
            if in_coset(ipx, ipz, lx, lz, stab_group):
                return name
        return '??'
    lx_name = classify_one(*img_lx)
    lz_name = classify_one(*img_lz)
    return lx_name, lz_name


# ── k>1 logical action (symplectic matrix) ────────────────────────────────────

def logical_matrix(images, logicals, k, stab_group):
    """
    Return a 2k×2k binary matrix M (list of rows) where M[i][j] = 1 means the
    j-th logical basis operator appears in the image of the i-th basis operator.
    Column/row ordering: X_0, Z_0, X_1, Z_1, ..., X_{k-1}, Z_{k-1}.
    Returns None in a row if the image is not in the normalizer.
    """
    n = len(logicals[0][0])
    basis = []
    for i in range(k):
        basis.append(logicals[2 * i])       # L_X_i
        basis.append(logicals[2 * i + 1])   # L_Z_i

    # Pre-compute physical representative for every 2k-bit mask (4^k entries)
    cand = {}  # mask -> (x, z)
    for mask in range(1 << (2 * k)):
        cx = [0] * n
        cz = [0] * n
        for b in range(2 * k):
            if mask & (1 << b):
                cx = xor(cx, basis[b][0])
                cz = xor(cz, basis[b][1])
        cand[mask] = (cx, cz)

    rows = []
    for img_x, img_z in images:
        found = None
        for mask, (cx, cz) in cand.items():
            if in_coset(img_x, img_z, cx, cz, stab_group):
                found = mask
                break
        if found is None:
            rows.append(None)
        else:
            rows.append([(found >> b) & 1 for b in range(2 * k)])
    return rows


def format_logical_matrix(M, k, gen_str):
    """Format a 2k×2k logical action matrix as a labelled grid."""
    labels = []
    for i in range(k):
        labels += [f'X{i}', f'Z{i}']
    w = max(len(g) for g in labels)

    lines = []
    # generator label
    lines.append(f'Generator: {gen_str}')
    # column header
    col_hdr = ' ' * (w + 2) + '  '.join(f'{l:>{w}}' for l in labels)
    lines.append(col_hdr)
    lines.append(' ' * (w + 2) + ('-' * (w) + '--') * (2 * k - 1) + '-' * w)

    for i, row in enumerate(M):
        if row is None:
            img_str = '??'
            bits = '?' * (2 * k)
        else:
            parts = [f'L_{labels[j]}' for j in range(2 * k) if row[j]]
            img_str = '*'.join(parts) if parts else 'I'
            bits = '  '.join(f'{b:{w}}' for b in row)
        lines.append(f'{labels[i]:>{w}}  {bits}    →  {img_str}')

    return '\n'.join(lines)


# ── database loading ──────────────────────────────────────────────────────────

def load_code(n, k, code_id):
    """Search all d-files for a [[n,k]] code with the given id."""
    pattern = str(DATA_ROOT / f"n_{n}" / f"codes_n_{n}_k_{k}_d_*.json")
    key = str(code_id)
    for fpath in sorted(glob.glob(pattern)):
        data = json.load(open(fpath))
        if key in data:
            return data[key], Path(fpath).name
    return None, None


# ── shared setup (importable API) ─────────────────────────────────────────────

def prepare_code_data(n, k, code_id):
    """Load a code and build the shared data structures needed for logical-action work.

    Returns (code, fname, interleaved, stab_group, cand) where:
      code        : raw dict from the database
      fname       : source filename
      interleaved : list of 2k (x_vec, z_vec) pairs [LX_0,LZ_0,LX_1,LZ_1,...]
      stab_group  : set of 2n-bit tuples representing all stabilizer group elements
      cand        : dict  mask -> (cx, cz)  for all 4^k logical Pauli products
    """
    code, fname = load_code(n, k, code_id)
    if code is None:
        raise ValueError(f"Code [[{n},{k}]] id={code_id} not found in database.")

    stab_symp = [parse_pauli(s, n) for s in code['isotropic_generators']]
    stab_group = build_stabilizer_group(stab_symp)

    raw = code['logical_ops']
    parsed = [parse_pauli(s, n) for s in raw]
    interleaved = []
    for i in range(k):
        interleaved.append(parsed[i])       # L_X_i
        interleaved.append(parsed[k + i])   # L_Z_i

    cand = {}
    for mask in range(1 << (2 * k)):
        cx = [0] * n
        cz = [0] * n
        for b in range(2 * k):
            if mask & (1 << b):
                cx = xor(cx, interleaved[b][0])
                cz = xor(cz, interleaved[b][1])
        cand[mask] = (cx, cz)

    return code, fname, interleaved, stab_group, cand


def generator_to_matrix(gen_str, k, n, interleaved, stab_group, cand):
    """Return the 2k×2k symplectic matrix (tuple of rows) for one generator string."""
    images = []
    for i in range(k):
        lx, lz = interleaved[2 * i], interleaved[2 * i + 1]
        images.append(apply_generator(gen_str, list(lx[0]), list(lx[1])))
        images.append(apply_generator(gen_str, list(lz[0]), list(lz[1])))

    rows = []
    for img_x, img_z in images:
        found = None
        for mask, (cx, cz) in cand.items():
            if in_coset(img_x, img_z, cx, cz, stab_group):
                found = mask
                break
        if found is None:
            rows.append(None)
        else:
            rows.append(tuple((found >> b) & 1 for b in range(2 * k)))
    return tuple(rows)


def get_logical_matrices(n, k, code_id):
    """Return (gen_matrices, aut_group_size, info) for a code.

    gen_matrices : list of 2k×2k tuple-of-tuples, one per aut_group_generator.
    aut_group_size : integer from the database.
    info : dict with keys n, k, d, id, fname, generators.

    This is the importable API for aut_logical_group.py.
    """
    code, fname, interleaved, stab_group, cand = prepare_code_data(n, k, code_id)
    mats = [
        generator_to_matrix(g, k, n, interleaved, stab_group, cand)
        for g in code['aut_group_generators']
    ]
    return mats, code.get('aut_group_size'), {
        'n': n, 'k': k, 'd': code.get('d', '?'),
        'id': code_id, 'fname': fname,
        'generators': code['aut_group_generators'],
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    n, k, code_id = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])

    try:
        code, fname, interleaved, stab_group, cand = prepare_code_data(n, k, code_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    d = code.get('d', '?')
    print(f"Code [[{n},{k},{d}]]  id={code_id}  (from {fname})")
    print(f"Automorphism group order: {code.get('aut_group_size','?')}")
    print()

    raw_logicals = code['logical_ops']
    print("Logical operators:")
    for i in range(k):
        print(f"  L_X_{i} = {raw_logicals[i]}")
        print(f"  L_Z_{i} = {raw_logicals[k+i]}")
    print()

    gens = code['aut_group_generators']

    if k == 1:
        LX, LZ = interleaved[0], interleaved[1]
        header = f"{'Generator':<28}  {'L_X →':<8}  {'L_Z →':<8}  Logical action"
        print(header)
        print("-" * len(header))
        for g in gens:
            img_lx = apply_generator(g, list(LX[0]), list(LX[1]))
            img_lz = apply_generator(g, list(LZ[0]), list(LZ[1]))
            lx_name, lz_name = classify_k1(img_lx, img_lz, LX, LZ, stab_group)
            action = K1_ACTION_NAMES.get((lx_name, lz_name), f'?? ({lx_name},{lz_name})')
            print(f"{g:<28}  {lx_name:<8}  {lz_name:<8}  {action}")
    else:
        for g in gens:
            images = []
            for i in range(k):
                lx, lz = interleaved[2*i], interleaved[2*i+1]
                images.append(apply_generator(g, list(lx[0]), list(lx[1])))
                images.append(apply_generator(g, list(lz[0]), list(lz[1])))
            M = logical_matrix(images, interleaved, k, stab_group)
            print(format_logical_matrix(M, k, g))
            print()

    print()


if __name__ == "__main__":
    main()
