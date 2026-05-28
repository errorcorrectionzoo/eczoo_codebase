# Validate the maximal-descent algorithm on a smaller Sp(dim,2).

if not IsBound(SCRIPT_DIR) then Error("SCRIPT_DIR must be bound"); fi;
if not IsBound(DIM) then DIM := 6; fi;
if not IsBound(VALIDATION_PATH) then
    VALIDATION_PATH := "sp8_validation.json";
fi;

Read(Concatenation(SCRIPT_DIR, "/sp8_common.g"));

ctx := SP8_BuildContext(DIM);;
P := ctx.P;;
direct := ConjugacyClassesSubgroups(P);;

seen := [ P ];;
queue := [ P ];;

while Length(queue) > 0 do
    H := Remove(queue, 1);;
    maximals := MaximalSubgroupClassReps(H);;
    for M in maximals do
        is_new := true;;
        for R in seen do
            if Order(R) = Order(M) and IsConjugate(P, R, M) then
                is_new := false;;
                break;
            fi;
        od;
        if is_new then
            Add(seen, M);;
            Add(queue, M);;
        fi;
    od;
od;

PrintTo(VALIDATION_PATH,
    "{",
    "\"dim\":", String(DIM), ",",
    "\"direct_classes\":", String(Length(direct)), ",",
    "\"recursive_classes\":", String(Length(seen)), ",",
    "\"match\":", String(Length(direct) = Length(seen)),
    "}\n"
);
QUIT_GAP(0);
