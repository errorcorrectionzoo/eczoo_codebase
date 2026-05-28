# Compute cached detail keys for a batch of stored subgroup representatives.

if not IsBound(SCRIPT_DIR) then Error("SCRIPT_DIR must be bound"); fi;
if not IsBound(DETAIL_INPUT) then Error("DETAIL_INPUT must be bound"); fi;
if not IsBound(DIM) then DIM := 8; fi;

Read(Concatenation(SCRIPT_DIR, "/sp8_common.g"));
Read(DETAIL_INPUT);

if IsExistingFile(SENTINEL_PATH) then
    QUIT_GAP(0);
fi;

ctx := SP8_BuildContext(DIM);;
P := ctx.P;;
degree := LargestMovedPoint(P);;
pair_points := Combinations([1..degree], 2);;

PrintTo(OUTPUT_PATH, "");

for item in ITEMS do
    H := SP8_ReadRep(item.rep_path, P);;
    detail_key := SP8_DetailKeyString(H, pair_points);;
    if item.kind = "rep" then
        AppendTo(OUTPUT_PATH,
            "{\"kind\":\"rep\",",
            "\"id\":", String(item.id), ",",
            "\"order\":", String(item.order), ",",
            "\"json_path\":", SP8_Quote(item.json_path), ",",
            "\"detail_key\":", SP8_Quote(detail_key),
            "}\n"
        );
    elif item.kind = "raw" then
        AppendTo(OUTPUT_PATH,
            "{\"kind\":\"raw\",",
            "\"raw_id\":", SP8_Quote(item.raw_id), ",",
            "\"order\":", String(item.order), ",",
            "\"children_path\":", SP8_Quote(item.children_path), ",",
            "\"detail_key\":", SP8_Quote(detail_key),
            "}\n"
        );
    else
        Error("unknown detail item kind");
    fi;
od;

PrintTo(SENTINEL_PATH, "ok\n");
QUIT_GAP(0);
