# Initialize a resumable Sp(8,2) subgroup-classification run directory.

if not IsBound(SCRIPT_DIR) then
    Error("SCRIPT_DIR must be bound");
fi;
if not IsBound(RUN_DIR) then
    Error("RUN_DIR must be bound");
fi;
if not IsBound(DIM) then
    DIM := 8;
fi;

Read(Concatenation(SCRIPT_DIR, "/sp8_common.g"));

ctx := SP8_BuildContext(DIM);;
P := ctx.P;;
degree := LargestMovedPoint(P);;
pair_points := Combinations([1..degree], 2);;

if Order(P) <> Order(Sp(DIM, 2)) then
    Error("permutation image has wrong order");
fi;
if Size(KernelOfMultiplicativeGeneralMapping(ctx.phi)) <> 1 then
    Error("permutation image is not faithful");
fi;

group_model_path := Concatenation(RUN_DIR, "/group_model.g");;
SP8_WriteGroupFile(group_model_path, "top_group_model", P);

incidence_path := Concatenation(RUN_DIR, "/incidence.jsonl");;
PrintTo(incidence_path, "");

top_rep_path := Concatenation(RUN_DIR, "/reps/rep_1.g");;
top_meta_path := Concatenation(RUN_DIR, "/reps/rep_1.json");;
SP8_WriteGroupFile(top_rep_path, 1, P);

maximals := MaximalSubgroupClassReps(P);;
SP8_WriteRepJSON(
    top_meta_path, 1, Order(P), top_rep_path, "processed", "root",
    0, Length(maximals), fail, SP8_FingerprintString(P, degree),
    SP8_DetailKeyString(P, pair_points)
);

for i in [1..Length(maximals)] do
    id := i + 1;;
    rep_path := Concatenation(RUN_DIR, "/reps/rep_", String(id), ".g");;
    meta_path := Concatenation(RUN_DIR, "/reps/rep_", String(id), ".json");;
    if i in [8, 9] then
        job_class := "heavy";;
    else
        job_class := "light";;
    fi;
    SP8_WriteGroupFile(rep_path, id, maximals[i]);
    SP8_WriteRepJSON(
        meta_path, id, Order(maximals[i]), rep_path, "queued", job_class,
        i, fail, 1, SP8_FingerprintString(maximals[i], degree),
        SP8_DetailKeyString(maximals[i], pair_points)
    );
    SP8_AppendIncidence(incidence_path, 1, id, "top_maximal");
od;

PrintTo(Concatenation(RUN_DIR, "/init.success"), "ok\n");
QUIT_GAP(0);
