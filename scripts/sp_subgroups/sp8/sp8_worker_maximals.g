# Worker: compute MaximalSubgroupClassReps for one representative.

if not IsBound(SCRIPT_DIR) then Error("SCRIPT_DIR must be bound"); fi;
if not IsBound(REP_ID) then Error("REP_ID must be bound"); fi;
if not IsBound(JOB_ID) then Error("JOB_ID must be bound"); fi;
if not IsBound(REP_PATH) then Error("REP_PATH must be bound"); fi;
if not IsBound(RAW_DIR) then Error("RAW_DIR must be bound"); fi;
if not IsBound(CHILDREN_JSONL) then Error("CHILDREN_JSONL must be bound"); fi;
if not IsBound(SENTINEL_PATH) then Error("SENTINEL_PATH must be bound"); fi;
if not IsBound(SUMMARY_PATH) then Error("SUMMARY_PATH must be bound"); fi;
if not IsBound(DIM) then DIM := 8; fi;

Read(Concatenation(SCRIPT_DIR, "/sp8_common.g"));

if IsExistingFile(SENTINEL_PATH) then
    QUIT_GAP(0);
fi;

ctx := SP8_BuildContext(DIM);;
P := ctx.P;;
degree := LargestMovedPoint(P);;
pair_points := Combinations([1..degree], 2);;
H := SP8_ReadRep(REP_PATH, P);;

maximals := MaximalSubgroupClassReps(H);;
PrintTo(CHILDREN_JSONL, "");

for i in [1..Length(maximals)] do
    raw_id := Concatenation(String(JOB_ID), "_", String(i));;
    child_path := Concatenation(RAW_DIR, "/child_", String(i), ".g");;
    fingerprint := SP8_FingerprintString(maximals[i], degree);;
    detail_key := SP8_DetailKeyString(maximals[i], pair_points);;
    SP8_WriteGroupFile(child_path, raw_id, maximals[i]);
    SP8_AppendRawChild(
        CHILDREN_JSONL, raw_id, REP_ID, i, Order(maximals[i]),
        child_path, fingerprint, detail_key, String(JOB_ID)
    );
od;

PrintTo(SUMMARY_PATH,
    "{",
    "\"job_id\":", SP8_Quote(String(JOB_ID)), ",",
    "\"rep_id\":", String(REP_ID), ",",
    "\"maximal_count\":", String(Length(maximals)), ",",
    "\"complete\":true",
    "}\n"
);
PrintTo(SENTINEL_PATH, "ok\n");
QUIT_GAP(0);
