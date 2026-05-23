# Classify subgroups of Sp(2n, 2) up to conjugacy within Sp(2n, 2).
#
# Conjugacy is relative to Sp(2n, 2) itself, NOT GL(2n, 2).
# For n=1 the two coincide (Sp(2,2) = GL(2,2) = S3).
# For n>=2 they differ: e.g. n=2 gives 56 Sp-classes vs 8 GL-classes.
#
# Usage:
#   gap sp_subgroups.g
#   gap -c 'n := 1;' sp_subgroups.g
#   gap -c 'n := 3; ENUM_LIMIT := 2000000;' sp_subgroups.g

SizeScreen([10000, 80]);   # prevent GAP from wrapping output lines

if not IsBound(n)          then n          := 2;     fi;
if not IsBound(ENUM_LIMIT) then ENUM_LIMIT := 10000; fi;
if not IsBound(GEN_LIMIT)  then GEN_LIMIT  := 8;     fi;

dim := 2 * n;
G   := Sp(dim, 2);

Print("G = Sp(", dim, ", 2),  |G| = ", Order(G), "\n");
Print("    ", StructureDescription(G), "\n\n");

if Order(G) > ENUM_LIMIT then
    Print("  |G| = ", Order(G), " > ENUM_LIMIT = ", ENUM_LIMIT, "; aborting.\n");
    Print("  Run:  gap -c 'n := ", n, "; ENUM_LIMIT := ", 2*Order(G), ";' sp_subgroups.g\n");
else

# ── Conjugacy classes within Sp(2n, 2) ───────────────────────────────────────
# ConjugacyClassesSubgroups(G) uses G = Sp as the ambient group, so two
# subgroups land in the same class iff they are conjugate by an Sp-element,
# NOT merely by a GL-element.

cc         := ConjugacyClassesSubgroups(G);
total_cc   := Length(cc);
total_subs := Sum(cc, function(x) return Size(x); end);

Print("Sp(", dim, ", 2)-conjugacy classes:  ", total_cc, "\n");
Print("Total subgroups:               ", total_subs, "\n\n");

# ── Helpers ───────────────────────────────────────────────────────────────────

# Left-justify a value in a field of width w (truncate if too long).
# ShallowCopy ensures we get a mutable string we can Append to.
Pad := function(val, w)
    local s;
    s := ShallowCopy(String(val));
    while Length(s) < w do Add(s, ' '); od;
    if Length(s) > w then s := s{[1..w]}; fi;
    return s;
end;

# Orders ≤ 2000 excluded from the SmallGroups library (too many groups to store).
SMALL_GROUPS_EXCLUDED := Set([512, 768, 1024, 1152, 1536, 1920]);

# Return [label_string, sort_order, sort_secondary] for the isomorphism type.
# Uses SmallGroup(n,k) when the order is in the database.
# Falls back to an algebraic fingerprint for excluded or larger orders.
# NOTE: the Nonabelian fallback is not a complete invariant for large orders.
IsoLabel := function(H)
    local ord, id, invs, s, i;
    ord := Order(H);
    if ord <= 2000 and not ord in SMALL_GROUPS_EXCLUDED then
        id := IdSmallGroup(H);
        return [ Concatenation("SmallGroup(", String(id[1]), ", ", String(id[2]), ")"),
                 id[1], id[2] ];
    elif IsAbelian(H) then
        invs := AbelianInvariants(H);
        s := ShallowCopy(String(invs[1]));
        for i in [2..Length(invs)] do
            Append(s, "+"); Append(s, String(invs[i]));
        od;
        return [ Concatenation("Abelian[", s, "]"), ord, 0 ];
    else
        return [ Concatenation("Nonabelian(ord=", String(ord), ")"), ord, 1 ];
    fi;
end;

# ── Accumulate by isomorphism type ───────────────────────────────────────────
# iso entry: [ label, ord, sec, cc_count, sub_total, struct_desc ]

iso := [];

for cls in cc do
    H    := Representative(cls);
    info := IsoLabel(H);
    if info[2] <= 2000 and not info[2] in SMALL_GROUPS_EXCLUDED then
        desc := StructureDescription(H);
    else
        desc := info[1];
    fi;

    pos := fail;
    for k in [1..Length(iso)] do
        if iso[k][1] = info[1] then pos := k; break; fi;
    od;
    if pos = fail then
        Add(iso, [ info[1], info[2], info[3], 1, Size(cls), desc ]);
    else
        iso[pos][4] := iso[pos][4] + 1;
        iso[pos][5] := iso[pos][5] + Size(cls);
    fi;
od;

Sort(iso, function(a, b)
    if   a[2] <> b[2] then return a[2] < b[2];
    elif a[3] <> b[3] then return a[3] < b[3];
    else                    return a[1] < b[1];
    fi;
end);

sep := "";
for i in [1..74] do Append(sep, "-"); od;

# ── Summary table ─────────────────────────────────────────────────────────────

Print("Isomorphism classes  (Sp(", dim, ", 2)-conjugacy):\n\n");
Print("  ", Pad("Isomorphism type",  30), "  ",
           Pad("Order",               6), "  ",
           Pad("Conj.classes",       12), "  ",
           Pad("#Subgroups",         10), "  Name\n");
Print("  ", sep, "\n");

for row in iso do
    Print("  ", Pad(row[1], 30), "  ", String(row[2],  6),
          "  ", String(row[4], 12), "  ", String(row[5], 10),
          "  ", row[6], "\n");
od;
Print("\n");

# ── Per-conjugacy-class listing ───────────────────────────────────────────────

Print("All Sp(", dim, ", 2)-conjugacy classes:\n\n");
cc_sorted := ShallowCopy(cc);
Sort(cc_sorted, function(a, b)
    return Order(Representative(a)) < Order(Representative(b));
end);

for cls in cc_sorted do
    H    := Representative(cls);
    info := IsoLabel(H);
    if info[2] <= 2000 and not info[2] in SMALL_GROUPS_EXCLUDED then
        desc := Concatenation(", ", StructureDescription(H));
    else
        desc := "";
    fi;
    Print("  ", info[1], "  (order ", Order(H),
          ", class size ", Size(cls), desc, ")\n");

    if Order(H) <= GEN_LIMIT then
        for gi in [1..Length(GeneratorsOfGroup(H))] do
            M := GeneratorsOfGroup(H)[gi];
            for r in [1..dim] do
                # Build row as a plain string to avoid GAP's list pretty-printer,
                # which wraps long lines with backslash continuations.
                rowstr := ShallowCopy("[");
                for col in [1..dim] do
                    if col > 1 then Append(rowstr, ", "); fi;
                    Append(rowstr, String(Int(M[r][col])));
                od;
                Append(rowstr, "]");
                if r = 1 then Print("    g", gi, " = ", rowstr, "\n");
                else           Print("         ", rowstr, "\n");
                fi;
            od;
        od;
    fi;
    Print("\n");
od;

fi; # end ENUM_LIMIT guard

quit;
