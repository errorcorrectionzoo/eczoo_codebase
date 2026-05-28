# Merge one order bucket of raw child representatives into top-level classes.

if not IsBound(SCRIPT_DIR) then Error("SCRIPT_DIR must be bound"); fi;
if not IsBound(MERGE_INPUT) then Error("MERGE_INPUT must be bound"); fi;
if not IsBound(DIM) then DIM := 8; fi;

Read(Concatenation(SCRIPT_DIR, "/sp8_common.g"));
Read(MERGE_INPUT);

if IsExistingFile(SENTINEL_PATH) then
    QUIT_GAP(0);
fi;

SP8_MergeKeyKnown := function(entry)
    return IsBound(entry.merge_key) and entry.merge_key <> "";
end;

SP8_MergeKeyCompatible := function(left, right)
    if not SP8_MergeKeyKnown(left) or not SP8_MergeKeyKnown(right) then
        return true;
    fi;
    if left.merge_key <> right.merge_key then
        return false;
    fi;
    if IsBound(left.detail_key) and IsBound(right.detail_key) and
       left.detail_key <> "" and right.detail_key <> "" then
        return left.detail_key = right.detail_key;
    fi;
    return true;
end;

SP8_GetDetailKey := function(entry, H, pair_points)
    if IsBound(entry.detail_key) and entry.detail_key <> "" then
        return entry.detail_key;
    fi;
    return SP8_DetailKeyString(H, pair_points);
end;

SP8_FindDetailBucket := function(buckets, key)
    local i;
    for i in [1..Length(buckets.known)] do
        if buckets.known[i].key = key then
            return buckets.known[i];
        fi;
    od;
    return fail;
end;

SP8_AddDetailBucketEntry := function(buckets, entry)
    local bucket;
    if IsBound(entry.detail_key) and entry.detail_key <> "" then
        bucket := SP8_FindDetailBucket(buckets, entry.detail_key);;
        if bucket = fail then
            bucket := rec(key := entry.detail_key, entries := []);;
            Add(buckets.known, bucket);
        fi;
        Add(bucket.entries, entry);
    else
        Add(buckets.unknown, entry);
    fi;
end;

SP8_DetailBucketEntries := function(buckets, key, all_entries)
    local bucket, entries;
    if key = "" then
        return all_entries;
    fi;
    entries := ShallowCopy(buckets.unknown);;
    bucket := SP8_FindDetailBucket(buckets, key);;
    if bucket <> fail then
        Append(entries, bucket.entries);
    fi;
    return entries;
end;

ctx := SP8_BuildContext(DIM);;
P := ctx.P;;
degree := LargestMovedPoint(P);;
pair_points := Combinations([1..degree], 2);;

existing_groups := [];;
existing_buckets := rec(known := [], unknown := []);;
for entry in EXISTING do
    H := SP8_ReadRep(entry.path, P);;
    loaded_entry := rec(
        id := entry.id,
        path := entry.path,
        fingerprint := entry.fingerprint,
        merge_key := entry.merge_key,
        detail_key := SP8_GetDetailKey(entry, H, pair_points),
        group := H
    );;
    Add(existing_groups, loaded_entry);
    SP8_AddDetailBucketEntry(existing_buckets, loaded_entry);
od;

accepted_groups := [];;
accepted_buckets := rec(known := [], unknown := []);;
PrintTo(PROPOSAL_PATH, "");
PrintTo(CACHE_PATH, "");
tests_existing := 0;;
tests_accepted := 0;;
skipped_existing := 0;;
skipped_accepted := 0;;
processed_candidates := 0;;

for cand in CANDIDATES do
    C := SP8_ReadRep(cand.path, P);;
    cand.detail_key := SP8_GetDetailKey(cand, C, pair_points);;
    matched := false;;
    target_id := fail;;
    target_temp := fail;;

    for entry in SP8_DetailBucketEntries(existing_buckets, cand.detail_key, existing_groups) do
        if SP8_MergeKeyCompatible(cand, entry) then
            tests_existing := tests_existing + 1;;
            is_conj := IsConjugate(P, C, entry.group);;
            AppendTo(CACHE_PATH,
                "{\"left\":", SP8_Quote(cand.raw_id),
                ",\"right\":", String(entry.id),
                ",\"result\":", String(is_conj),
                "}\n"
            );
            if is_conj then
                matched := true;;
                target_id := entry.id;;
                break;
            fi;
        else
            skipped_existing := skipped_existing + 1;;
        fi;
    od;

    if not matched then
        for entry in SP8_DetailBucketEntries(accepted_buckets, cand.detail_key, accepted_groups) do
            if SP8_MergeKeyCompatible(cand, entry) then
                tests_accepted := tests_accepted + 1;;
                is_conj := IsConjugate(P, C, entry.group);;
                AppendTo(CACHE_PATH,
                    "{\"left\":", SP8_Quote(cand.raw_id),
                    ",\"right\":", SP8_Quote(entry.temp_id),
                    ",\"result\":", String(is_conj),
                    "}\n"
                );
                if is_conj then
                    matched := true;;
                    target_temp := entry.temp_id;;
                    break;
                fi;
            else
                skipped_accepted := skipped_accepted + 1;;
            fi;
        od;
    fi;

    if matched then
        if target_id = fail then
            AppendTo(PROPOSAL_PATH,
                "{\"kind\":\"identified\",",
                "\"raw_id\":", SP8_Quote(cand.raw_id), ",",
                "\"parent_id\":", String(cand.parent_id), ",",
                "\"child_index\":", String(cand.child_index), ",",
                "\"order\":", String(cand.order), ",",
                "\"target_kind\":\"new\",",
                "\"target_temp_id\":", SP8_Quote(target_temp),
                "}\n"
            );
        else
            AppendTo(PROPOSAL_PATH,
                "{\"kind\":\"identified\",",
                "\"raw_id\":", SP8_Quote(cand.raw_id), ",",
                "\"parent_id\":", String(cand.parent_id), ",",
                "\"child_index\":", String(cand.child_index), ",",
                "\"order\":", String(cand.order), ",",
                "\"target_kind\":\"existing\",",
                "\"target_id\":", String(target_id),
                "}\n"
            );
        fi;
    else
        temp_id := Concatenation("new_", String(Length(accepted_groups) + 1));;
        accepted_entry := rec(
            temp_id := temp_id,
            group := C,
            path := cand.path,
            fingerprint := cand.fingerprint,
            merge_key := cand.merge_key,
            detail_key := cand.detail_key
        );;
        Add(accepted_groups, accepted_entry);
        SP8_AddDetailBucketEntry(accepted_buckets, accepted_entry);
        AppendTo(PROPOSAL_PATH,
            "{\"kind\":\"new\",",
            "\"temp_id\":", SP8_Quote(temp_id), ",",
            "\"raw_id\":", SP8_Quote(cand.raw_id), ",",
            "\"parent_id\":", String(cand.parent_id), ",",
            "\"child_index\":", String(cand.child_index), ",",
            "\"order\":", String(cand.order), ",",
            "\"raw_path\":", SP8_Quote(cand.path), ",",
            "\"fingerprint\":", SP8_Quote(cand.fingerprint), ",",
            "\"detail_key\":", SP8_Quote(cand.detail_key),
            "}\n"
        );
    fi;
    processed_candidates := processed_candidates + 1;;
    if processed_candidates mod 100 = 0 then
        Print(
            "MERGE_PROGRESS order=", String(ORDER),
            " processed=", String(processed_candidates),
            " candidates=", String(Length(CANDIDATES)),
            " accepted=", String(Length(accepted_groups)),
            " tests_existing=", String(tests_existing),
            " tests_accepted=", String(tests_accepted),
            " skipped_existing=", String(skipped_existing),
            " skipped_accepted=", String(skipped_accepted),
            "\n"
        );
    fi;
od;

Print(
    "MERGE_STATS order=", String(ORDER),
    " candidates=", String(Length(CANDIDATES)),
    " existing=", String(Length(EXISTING)),
    " tests_existing=", String(tests_existing),
    " tests_accepted=", String(tests_accepted),
    " skipped_existing=", String(skipped_existing),
    " skipped_accepted=", String(skipped_accepted),
    "\n"
);
PrintTo(SENTINEL_PATH, "ok\n");
QUIT_GAP(0);
