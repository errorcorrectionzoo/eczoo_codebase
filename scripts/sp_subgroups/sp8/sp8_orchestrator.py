#!/usr/bin/env python3
"""Orchestrator for the Sp(8,2) subgroup-classification GAP workflow."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GAP = Path("/home/valbert/gap-4.15.1/gap")
SCHEMA_VERSION = 1
CURRENT_DETAIL_PREFIX = "detail_v2;"
SEQUENCE_LOCK = threading.Lock()


CLASS_LIMITS = {
    "light": {"cap": "1g", "seconds": 30 * 60, "reservation_gb": 1},
    "medium": {"cap": "4g", "seconds": 3 * 60 * 60, "reservation_gb": 4},
    "heavy": {"cap": "16g", "seconds": 12 * 60 * 60, "reservation_gb": 16},
    "orthogonal_fallback": {"cap": "24g", "seconds": 24 * 60 * 60, "reservation_gb": 24},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kwargs)


def gap_string(value: str | Path) -> str:
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def merge_key_from_fingerprint(fingerprint: str | None) -> str:
    """Return a safe conjugacy-invariant key derived from the stored fingerprint.

    The current fingerprint also stores the number of generators GAP happened to
    use for the subgroup. That is useful metadata, but it is not a reliable
    conjugacy invariant, so do not use it to reject possible matches.
    """
    if not fingerprint:
        return ""
    parts = [part for part in str(fingerprint).split(";") if not part.startswith("gens=")]
    return ";".join(parts)


def merge_key_label(merge_key: str) -> str:
    if not merge_key:
        return "unknown"
    return hashlib.sha1(merge_key.encode()).hexdigest()[:12]


def current_detail_key(value: object) -> str:
    value = str(value or "")
    if value.startswith(CURRENT_DETAIL_PREFIX):
        return value
    return ""


def has_current_detail_key(value: object) -> bool:
    return bool(current_detail_key(value))


def detail_key_label(detail_key: str) -> str:
    detail_key = current_detail_key(detail_key)
    if not detail_key:
        return "mixed"
    return hashlib.sha1(detail_key.encode()).hexdigest()[:12]


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: dict) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def ensure_dirs(run_dir: Path) -> None:
    for name in [
        "snapshots",
        "reps",
        "jobs",
        "raw_children",
        "merge",
        "caches",
        "logs",
        "validation",
        "export",
    ]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)


def get_meminfo_gib() -> tuple[float, float]:
    total = available = 0
    with Path("/proc/meminfo").open() as f:
        for line in f:
            parts = line.split()
            if parts[0] == "MemTotal:":
                total = int(parts[1]) / 1024 / 1024
            elif parts[0] == "MemAvailable:":
                available = int(parts[1]) / 1024 / 1024
    return total, available


def get_nproc() -> int:
    return int(run_cmd(["nproc"], stdout=subprocess.PIPE, check=True).stdout.strip())


def gap_version(gap: Path) -> str:
    out = run_cmd([str(gap), "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout
    return out.strip()


def preflight(gap: Path = DEFAULT_GAP, strict: bool = True) -> dict:
    if not gap.exists():
        raise SystemExit(f"GAP binary not found: {gap}")
    help_text = run_cmd([str(gap), "-h"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout
    if "-K" not in help_text or "limitworkspace" not in help_text:
        raise SystemExit("GAP does not advertise -K / --limitworkspace")

    test = run_cmd(
        [
            "timeout",
            "10s",
            str(gap),
            "-q",
            "--quitonbreak",
            "-K",
            "1g",
            "-c",
            'Error("intentional preflight error");',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if test.returncode == 0:
        raise SystemExit("--quitonbreak preflight failed: intentional GAP error exited 0")

    for tool in ["/usr/bin/time", "timeout", "flock"]:
        if shutil.which(tool) is None and not Path(tool).exists():
            raise SystemExit(f"required helper not found: {tool}")

    total_gib, available_gib = get_meminfo_gib()
    nproc = get_nproc()
    disk = shutil.disk_usage(SCRIPT_DIR)
    disk_free_gb = disk.free / 1024**3
    if strict and nproc < 28:
        raise SystemExit(f"only {nproc} logical CPUs visible")
    if strict and total_gib < 50:
        raise SystemExit(f"only {total_gib:.1f} GiB RAM visible")
    if strict and disk_free_gb < 50:
        raise SystemExit(f"only {disk_free_gb:.1f} GB free at {SCRIPT_DIR}")

    return {
        "gap": str(gap),
        "gap_version": gap_version(gap),
        "nproc": nproc,
        "ram_total_gib": round(total_gib, 2),
        "ram_available_gib": round(available_gib, 2),
        "ram_work_gib": min(48, max(1, int(total_gib) - 10)),
        "disk_free_gb": round(disk_free_gb, 2),
        "checked_at": utc_now(),
    }


def load_manifest(run_dir: Path) -> dict:
    return read_json(run_dir / "manifest.json")


def rep_json_path(run_dir: Path, rep_id: int) -> Path:
    return run_dir / "reps" / f"rep_{rep_id}.json"


def rep_gap_path(run_dir: Path, rep_id: int) -> Path:
    return run_dir / "reps" / f"rep_{rep_id}.g"


def load_reps(run_dir: Path) -> dict[int, dict]:
    reps = {}
    for path in sorted((run_dir / "reps").glob("rep_*.json")):
        meta = read_json(path)
        reps[int(meta["id"])] = meta
    return reps


def load_rep(run_dir: Path, rep_id: int) -> dict:
    return read_json(rep_json_path(run_dir, rep_id))


def save_rep(run_dir: Path, meta: dict) -> None:
    atomic_write_json(rep_json_path(run_dir, int(meta["id"])), meta)


def next_sequence(run_dir: Path, name: str) -> int:
    with SEQUENCE_LOCK:
        path = run_dir / f".next_{name}_id"
        if path.exists():
            value = int(path.read_text().strip())
        else:
            if name == "rep":
                reps = load_reps(run_dir)
                value = max(reps.keys(), default=0) + 1
            else:
                value = 1
        atomic_write(path, f"{value + 1}\n")
        return value


def snapshot(run_dir: Path, reason: str) -> Path:
    reps = load_reps(run_dir)
    snap_dir = run_dir / "snapshots"
    existing = sorted(snap_dir.glob("progress_*.json"))
    snap_id = len(existing)
    counts = Counter(meta.get("status", "unknown") for meta in reps.values())
    classes = Counter(meta.get("job_class", "unknown") for meta in reps.values())
    frontier = sorted(rid for rid, meta in reps.items() if meta.get("status") in {"queued", "new"})
    raw_backlog = len(unmerged_raw_jobs(run_dir))
    progress = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snap_id,
        "reason": reason,
        "written_at": utc_now(),
        "representative_count": len(reps),
        "status_counts": dict(counts),
        "job_class_counts": dict(classes),
        "frontier_ids": frontier,
        "raw_child_backlog": raw_backlog,
    }
    progress_path = snap_dir / f"progress_{snap_id:06d}.json"
    atomic_write_json(progress_path, progress)
    atomic_write(snap_dir / "latest", progress_path.name + "\n")
    with (snap_dir / f"reps_{snap_id:06d}.jsonl").open("w") as f:
        for rid in sorted(reps):
            f.write(json.dumps(reps[rid], sort_keys=True) + "\n")
    incidence = run_dir / "incidence.jsonl"
    if incidence.exists():
        shutil.copy2(incidence, snap_dir / f"incidence_{snap_id:06d}.jsonl")
    with (snap_dir / f"frontier_{snap_id:06d}.jsonl").open("w") as f:
        for rid in frontier:
            f.write(json.dumps({"id": rid}, sort_keys=True) + "\n")
    return progress_path


def cmd_preflight(args: argparse.Namespace) -> None:
    info = preflight(Path(args.gap), strict=not args.reduced)
    print(json.dumps(info, indent=2, sort_keys=True))


def cmd_init(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if (run_dir / "manifest.json").exists() and not args.force:
        raise SystemExit(f"run directory already initialized: {run_dir}")
    ensure_dirs(run_dir)
    info = preflight(Path(args.gap), strict=not args.reduced)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "created_at": utc_now(),
        "dim": args.dim,
        "script_dir": str(SCRIPT_DIR),
        "class_limits": CLASS_LIMITS,
        "ordinary_workers": args.ordinary_workers,
        "heavy_workers": args.heavy_workers,
        **info,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)

    expr = (
        f"SCRIPT_DIR:={gap_string(SCRIPT_DIR)};;"
        f"RUN_DIR:={gap_string(run_dir)};;"
        f"DIM:={args.dim};;"
    )
    cmd = [
        str(Path(args.gap)),
        "-q",
        "--quitonbreak",
        "-K",
        "1g",
        "-c",
        expr,
        str(SCRIPT_DIR / "sp8_init.g"),
    ]
    subprocess.run(cmd, check=True)
    snapshot(run_dir, "init")
    print(f"initialized {run_dir}")


def parse_time_file(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    text = path.read_text(errors="replace")
    match = re.search(r"Maximum resident set size \(kbytes\):\s+(\d+)", text)
    if match:
        data["max_rss_kb"] = int(match.group(1))
    match = re.search(r"Elapsed \(wall clock\) time.*:\s+([^\n]+)", text)
    if match:
        data["elapsed"] = match.group(1).strip()
    return data


def limit_for(job_class: str) -> dict:
    return CLASS_LIMITS.get(job_class, CLASS_LIMITS["light"])


def run_worker(run_dir: Path, manifest: dict, rep_id: int) -> dict:
    meta = load_rep(run_dir, rep_id)
    job_id = str(next_sequence(run_dir, "job"))
    raw_dir = run_dir / "raw_children" / f"job_{job_id}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    children_jsonl = raw_dir / "children.jsonl"
    sentinel = raw_dir / "SUCCESS"
    summary = raw_dir / "summary.json"
    time_path = run_dir / "logs" / f"worker_{job_id}.time"
    stdout_path = run_dir / "logs" / f"worker_{job_id}.stdout"
    stderr_path = run_dir / "logs" / f"worker_{job_id}.stderr"
    job_class = meta.get("job_class", "light")
    limits = limit_for(job_class)
    rep_path = Path(meta["rep_path"])
    expr = (
        f"SCRIPT_DIR:={gap_string(SCRIPT_DIR)};;"
        f"RUN_DIR:={gap_string(run_dir)};;"
        f"DIM:={manifest['dim']};;"
        f"REP_ID:={rep_id};;"
        f"JOB_ID:={gap_string(job_id)};;"
        f"REP_PATH:={gap_string(rep_path)};;"
        f"RAW_DIR:={gap_string(raw_dir)};;"
        f"CHILDREN_JSONL:={gap_string(children_jsonl)};;"
        f"SENTINEL_PATH:={gap_string(sentinel)};;"
        f"SUMMARY_PATH:={gap_string(summary)};;"
    )
    cmd = [
        "/usr/bin/time",
        "-v",
        "-o",
        str(time_path),
        "timeout",
        f"{limits['seconds']}s",
        manifest["gap"],
        "-q",
        "--quitonbreak",
        "-K",
        limits["cap"],
        "-c",
        expr,
        str(SCRIPT_DIR / "sp8_worker_maximals.g"),
    ]
    started = utc_now()
    append_jsonl(
        run_dir / "jobs" / "running.jsonl",
        {
            "job_id": job_id,
            "type": "maximals",
            "rep_id": rep_id,
            "command": cmd,
            "started_at": started,
            "job_class": job_class,
            "workspace_cap": limits["cap"],
            "wall_limit_seconds": limits["seconds"],
        },
    )
    meta["status"] = "processing"
    meta["processing_job_id"] = job_id
    save_rep(run_dir, meta)

    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        proc = subprocess.run(cmd, stdout=stdout, stderr=stderr)
    resource = parse_time_file(time_path)
    result = {
        "job_id": job_id,
        "rep_id": rep_id,
        "returncode": proc.returncode,
        "sentinel": sentinel.exists(),
        "summary_path": str(summary),
        "children_jsonl": str(children_jsonl),
        "finished_at": utc_now(),
        **resource,
    }
    meta = load_rep(run_dir, rep_id)
    if proc.returncode == 0 and sentinel.exists() and summary.exists():
        summary_data = read_json(summary)
        meta["status"] = "processed"
        meta["maximal_count"] = summary_data.get("maximal_count")
        meta["last_job_id"] = job_id
        meta["last_peak_rss_kb"] = result.get("max_rss_kb")
        save_rep(run_dir, meta)
        append_jsonl(run_dir / "jobs" / "completed.jsonl", result)
    else:
        meta["status"] = "queued"
        meta["last_failure"] = "timeout" if proc.returncode == 124 else "nonzero_exit"
        meta["last_returncode"] = proc.returncode
        meta["last_job_id"] = job_id
        if job_class == "light":
            meta["job_class"] = "medium"
        elif job_class == "medium":
            meta["job_class"] = "heavy"
        save_rep(run_dir, meta)
        append_jsonl(run_dir / "jobs" / "failed.jsonl", result)
    return result


def unmerged_raw_jobs(run_dir: Path) -> list[Path]:
    jobs = []
    for raw_dir in sorted((run_dir / "raw_children").glob("job_*")):
        if (raw_dir / "SUCCESS").exists() and not (raw_dir / "MERGED").exists():
            jobs.append(raw_dir)
    return jobs


def write_merge_input(path: Path, dim: int, order: int, existing: list[dict], candidates: list[dict],
                      proposal_path: Path, cache_path: Path, sentinel_path: Path) -> None:
    def rec_existing(item: dict) -> str:
        fingerprint = item.get("fingerprint", "")
        detail_key = current_detail_key(item.get("detail_key", ""))
        return (
            "rec("
            f"id:={item['id']}, "
            f"path:={gap_string(item['rep_path'])}, "
            f"fingerprint:={gap_string(fingerprint)}, "
            f"merge_key:={gap_string(merge_key_from_fingerprint(fingerprint))}, "
            f"detail_key:={gap_string(detail_key)}"
            ")"
        )

    def rec_candidate(item: dict) -> str:
        fingerprint = item.get("fingerprint", "")
        detail_key = current_detail_key(item.get("detail_key", ""))
        return (
            "rec("
            f"raw_id:={gap_string(item['raw_id'])}, "
            f"parent_id:={item['parent_id']}, "
            f"child_index:={item['child_index']}, "
            f"order:={item['order']}, "
            f"path:={gap_string(item['rep_path'])}, "
            f"fingerprint:={gap_string(fingerprint)}, "
            f"merge_key:={gap_string(merge_key_from_fingerprint(fingerprint))}, "
            f"detail_key:={gap_string(detail_key)}"
            ")"
        )

    text = [
        f"DIM := {dim};;",
        f"ORDER := {order};;",
        "EXISTING := [",
        ",\n".join("  " + rec_existing(item) for item in existing),
        "];;",
        "CANDIDATES := [",
        ",\n".join("  " + rec_candidate(item) for item in candidates),
        "];;",
        f"PROPOSAL_PATH := {gap_string(proposal_path)};;",
        f"CACHE_PATH := {gap_string(cache_path)};;",
        f"SENTINEL_PATH := {gap_string(sentinel_path)};;",
    ]
    atomic_write(path, "\n".join(text) + "\n")


def bounded_merge_workers(
    requested: int,
    bucket_count: int,
    reservation_gb: float,
    reserve_ram_gb: float,
) -> int:
    if bucket_count <= 0:
        return 0
    requested = max(1, requested)
    try:
        nproc = get_nproc()
    except Exception:
        nproc = requested
    try:
        _, available_gib = get_meminfo_gib()
    except Exception:
        available_gib = 0

    memory_slots = requested
    if available_gib > 0:
        # Keep enough space for the OS, the orchestrator, filesystem cache, and
        # a few GAP processes that momentarily exceed their typical working set.
        memory_slots = max(1, int((available_gib - reserve_ram_gb) // reservation_gb))
    cpu_slots = max(1, nproc)
    return max(1, min(requested, bucket_count, memory_slots, cpu_slots))


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
        except Exception:
            return True
        return state != "Z"
    return True


def live_master_pid(run_dir: Path) -> int | None:
    path = run_dir / "master.pid"
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return None
    if process_alive(pid):
        return pid
    return None


def parse_order_filter(value: str | None) -> set[int] | None:
    if not value:
        return None
    return {int(part) for part in value.split(",") if part.strip()}


def wanted_detail_order(order: int, max_order: int, orders: set[int] | None) -> bool:
    if order > max_order:
        return False
    if orders is not None and order not in orders:
        return False
    return True


def iter_detail_backfill_items(
    run_dir: Path,
    include_raw: bool,
    include_merged_raw: bool,
    max_order: int,
    orders: set[int] | None,
):
    for path in sorted((run_dir / "reps").glob("rep_*.json")):
        meta = read_json(path)
        order = int(meta["order"])
        if has_current_detail_key(meta.get("detail_key")) or not wanted_detail_order(order, max_order, orders):
            continue
        yield {
            "kind": "rep",
            "id": int(meta["id"]),
            "order": order,
            "rep_path": meta["rep_path"],
            "json_path": str(path),
        }
    if not include_raw:
        return
    for raw_dir in sorted((run_dir / "raw_children").glob("job_*")):
        if not (raw_dir / "SUCCESS").exists():
            continue
        if (raw_dir / "MERGED").exists() and not include_merged_raw:
            continue
        children_path = raw_dir / "children.jsonl"
        for child in read_jsonl(children_path):
            order = int(child["order"])
            if has_current_detail_key(child.get("detail_key")) or not wanted_detail_order(order, max_order, orders):
                continue
            yield {
                "kind": "raw",
                "raw_id": child["raw_id"],
                "order": order,
                "rep_path": child["rep_path"],
                "children_path": str(children_path),
            }


def write_detail_batch_input(path: Path, items: list[dict], output_path: Path, sentinel_path: Path) -> None:
    def rec_item(item: dict) -> str:
        if item["kind"] == "rep":
            return (
                "rec("
                "kind:=\"rep\", "
                f"id:={item['id']}, "
                f"order:={item['order']}, "
                f"rep_path:={gap_string(item['rep_path'])}, "
                f"json_path:={gap_string(item['json_path'])}"
                ")"
            )
        return (
            "rec("
            "kind:=\"raw\", "
            f"raw_id:={gap_string(item['raw_id'])}, "
            f"order:={item['order']}, "
            f"rep_path:={gap_string(item['rep_path'])}, "
            f"children_path:={gap_string(item['children_path'])}"
            ")"
        )

    text = [
        "ITEMS := [",
        ",\n".join("  " + rec_item(item) for item in items),
        "];;",
        f"OUTPUT_PATH := {gap_string(output_path)};;",
        f"SENTINEL_PATH := {gap_string(sentinel_path)};;",
    ]
    atomic_write(path, "\n".join(text) + "\n")


def run_detail_batch(
    run_dir: Path,
    manifest: dict,
    batch_index: int,
    items: list[dict],
    cap: str,
    seconds: int,
) -> dict:
    batch_dir = run_dir / "detail_backfill" / f"batch_{time.time_ns()}_{batch_index:06d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    input_path = batch_dir / "input.g"
    output_path = batch_dir / "details.jsonl"
    sentinel_path = batch_dir / "SUCCESS"
    time_path = batch_dir / "time.log"
    stdout_path = batch_dir / "stdout.log"
    stderr_path = batch_dir / "stderr.log"
    write_detail_batch_input(input_path, items, output_path, sentinel_path)
    expr = (
        f"SCRIPT_DIR:={gap_string(SCRIPT_DIR)};;"
        f"DETAIL_INPUT:={gap_string(input_path)};;"
        f"DIM:={manifest['dim']};;"
    )
    cmd = [
        "/usr/bin/time",
        "-v",
        "-o",
        str(time_path),
        "timeout",
        f"{seconds}s",
        manifest["gap"],
        "-q",
        "--quitonbreak",
        "-K",
        cap,
        "-c",
        expr,
        str(SCRIPT_DIR / "sp8_detail_batch.g"),
    ]
    started = utc_now()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        proc = subprocess.run(cmd, stdout=stdout, stderr=stderr)
    return {
        "type": "detail_backfill",
        "batch_index": batch_index,
        "item_count": len(items),
        "returncode": proc.returncode,
        "sentinel": sentinel_path.exists(),
        "details_path": str(output_path),
        "batch_dir": str(batch_dir),
        "started_at": started,
        "finished_at": utc_now(),
        **parse_time_file(time_path),
    }


def apply_detail_records(records: list[dict]) -> dict:
    rep_updates = 0
    raw_updates = 0
    raw_by_file: dict[Path, dict[str, str]] = defaultdict(dict)
    for rec in records:
        detail_key = rec.get("detail_key", "")
        if not has_current_detail_key(detail_key):
            continue
        if rec.get("kind") == "rep":
            path = Path(rec["json_path"])
            if not path.exists():
                continue
            meta = read_json(path)
            if has_current_detail_key(meta.get("detail_key")):
                continue
            meta["detail_key"] = detail_key
            atomic_write_json(path, meta)
            rep_updates += 1
        elif rec.get("kind") == "raw":
            raw_by_file[Path(rec["children_path"])][rec["raw_id"]] = detail_key
    for path, detail_by_raw_id in raw_by_file.items():
        if not path.exists():
            continue
        changed = 0
        records_out = []
        for child in read_jsonl(path):
            raw_id = child.get("raw_id")
            if raw_id in detail_by_raw_id and not has_current_detail_key(child.get("detail_key")):
                child["detail_key"] = detail_by_raw_id[raw_id]
                changed += 1
            records_out.append(child)
        if changed:
            atomic_write(path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in records_out))
            raw_updates += changed
    return {"rep_updates": rep_updates, "raw_updates": raw_updates}


def apply_existing_detail_backfill(run_dir: Path) -> dict:
    records = []
    backfill_dir = run_dir / "detail_backfill"
    for path in sorted(backfill_dir.glob("batch_*/details.jsonl")):
        if (path.parent / "SUCCESS").exists():
            records.extend(read_jsonl(path))
    if not records:
        return {"rep_updates": 0, "raw_updates": 0}
    return apply_detail_records(records)


def cmd_backfill_details(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    manifest = load_manifest(run_dir)
    live_pid = live_master_pid(run_dir)
    if live_pid is not None and not args.force_live:
        raise SystemExit(
            f"orchestrator appears to be running as PID {live_pid}; "
            "stop it before writing detail keys, or pass --force-live if you are sure"
        )
    (run_dir / "detail_backfill").mkdir(parents=True, exist_ok=True)
    applied_existing = apply_existing_detail_backfill(run_dir)
    orders = parse_order_filter(args.orders)
    items = list(
        iter_detail_backfill_items(
            run_dir,
            include_raw=not args.skip_raw,
            include_merged_raw=args.include_merged_raw,
            max_order=args.max_order,
            orders=orders,
        )
    )
    if args.limit:
        items = items[: args.limit]
    chunks = [items[i:i + args.chunk_size] for i in range(0, len(items), args.chunk_size)]
    if not chunks:
        print(json.dumps({"pending": 0, "applied_existing": applied_existing}, sort_keys=True))
        return
    workers = bounded_merge_workers(args.workers, len(chunks), args.reservation_gb, args.reserve_ram_gb)
    summary = {
        "pending": len(items),
        "chunks": len(chunks),
        "workers": workers,
        "chunk_size": args.chunk_size,
        "applied_existing": applied_existing,
    }
    print(json.dumps(summary, sort_keys=True))
    results_path = run_dir / "detail_backfill" / "runs.jsonl"
    totals = Counter()
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_index = {
            pool.submit(run_detail_batch, run_dir, manifest, i, chunk, args.cap, args.seconds): i
            for i, chunk in enumerate(chunks, start=1)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            result = future.result()
            if result["returncode"] == 0 and result["sentinel"]:
                applied = apply_detail_records(read_jsonl(Path(result["details_path"])))
                result["applied"] = applied
                totals.update(applied)
            else:
                failures += 1
            append_jsonl(results_path, result)
            print(json.dumps(result, sort_keys=True))
    final = {
        "finished_at": utc_now(),
        "failures": failures,
        "rep_updates": totals["rep_updates"],
        "raw_updates": totals["raw_updates"],
    }
    atomic_write_json(run_dir / "detail_backfill" / f"summary_{int(time.time())}.json", final)
    print(json.dumps(final, sort_keys=True))


def prepare_merge_task(
    run_dir: Path,
    manifest: dict,
    order: int,
    merge_key: str,
    detail_key: str,
    existing: list[dict],
    candidates: list[dict],
    raw_dirs: set[Path],
) -> dict:
    candidate_keys = {merge_key_from_fingerprint(item.get("fingerprint", "")) for item in candidates}
    if "" not in candidate_keys:
        existing = [
            item for item in existing
            if merge_key_from_fingerprint(item.get("fingerprint", "")) in candidate_keys
            or merge_key_from_fingerprint(item.get("fingerprint", "")) == ""
        ]
    candidate_detail_keys = {current_detail_key(item.get("detail_key", "")) for item in candidates}
    if candidate_detail_keys and "" not in candidate_detail_keys:
        existing = [
            item for item in existing
            if current_detail_key(item.get("detail_key", "")) in candidate_detail_keys
            or current_detail_key(item.get("detail_key", "")) == ""
        ]
    key_label = merge_key_label(merge_key)
    detail_label = detail_key_label(detail_key)
    merge_id = f"{time.time_ns()}_{order}_{key_label}_{detail_label}_{len(candidates)}"
    merge_dir = run_dir / "merge" / f"bucket_{order}_{merge_id}"
    merge_dir.mkdir(parents=True, exist_ok=True)
    input_path = merge_dir / "input.g"
    proposal_path = merge_dir / "proposals.jsonl"
    cache_path = merge_dir / "conjugacy_cache.jsonl"
    sentinel_path = merge_dir / "SUCCESS"
    write_merge_input(
        input_path,
        manifest["dim"],
        order,
        existing,
        candidates,
        proposal_path,
        cache_path,
        sentinel_path,
    )
    expr = f"SCRIPT_DIR:={gap_string(SCRIPT_DIR)};;MERGE_INPUT:={gap_string(input_path)};;DIM:={manifest['dim']};;"
    limits = CLASS_LIMITS["medium"]
    time_path = merge_dir / "time.log"
    stdout_path = merge_dir / "stdout.log"
    stderr_path = merge_dir / "stderr.log"
    cmd = [
        "/usr/bin/time",
        "-v",
        "-o",
        str(time_path),
        "timeout",
        f"{limits['seconds']}s",
        manifest["gap"],
        "-q",
        "--quitonbreak",
        "-K",
        limits["cap"],
        "-c",
        expr,
        str(SCRIPT_DIR / "sp8_merge_bucket.g"),
    ]
    return {
        "type": "merge_bucket",
        "order": order,
        "merge_key": merge_key,
        "merge_key_label": key_label,
        "detail_key_label": detail_label,
        "candidate_count": len(candidates),
        "existing_count": len(existing),
        "merge_dir": merge_dir,
        "input_path": input_path,
        "proposal_path": proposal_path,
        "cache_path": cache_path,
        "sentinel_path": sentinel_path,
        "time_path": time_path,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "raw_dirs": raw_dirs,
        "cmd": cmd,
    }


def run_merge_task(task: dict) -> dict:
    started = utc_now()
    with task["stdout_path"].open("w") as stdout, task["stderr_path"].open("w") as stderr:
        proc = subprocess.run(task["cmd"], stdout=stdout, stderr=stderr)
    resource = parse_time_file(task["time_path"])
    return {
        "type": "merge_bucket",
        "order": task["order"],
        "merge_key": task["merge_key"],
        "merge_key_label": task["merge_key_label"],
        "detail_key_label": task["detail_key_label"],
        "candidate_count": task["candidate_count"],
        "existing_count": task["existing_count"],
        "returncode": proc.returncode,
        "sentinel": task["sentinel_path"].exists(),
        "proposal_path": str(task["proposal_path"]),
        "cache_path": str(task["cache_path"]),
        "merge_dir": str(task["merge_dir"]),
        "started_at": started,
        "finished_at": utc_now(),
        **resource,
    }


def merge_unmerged(
    run_dir: Path,
    manifest: dict,
    merge_workers: int = 8,
    merge_reservation_gb: float = 4.0,
    reserve_ram_gb: float = 8.0,
    detail_split_min: int = 256,
) -> int:
    raw_jobs = unmerged_raw_jobs(run_dir)
    if not raw_jobs:
        return 0
    candidates_by_order: dict[int, list[dict]] = defaultdict(list)
    raw_dirs_by_order: dict[int, set[Path]] = defaultdict(set)
    candidates_by_key_bucket: dict[tuple[int, str], list[dict]] = defaultdict(list)
    raw_dirs_by_key_bucket: dict[tuple[int, str], set[Path]] = defaultdict(set)
    candidates_by_bucket: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    raw_dirs_by_bucket: dict[tuple[int, str, str], set[Path]] = defaultdict(set)
    failed_raw_dirs: set[Path] = set()
    for raw_dir in raw_jobs:
        for child in read_jsonl(raw_dir / "children.jsonl"):
            order = int(child["order"])
            child["_raw_dir"] = str(raw_dir)
            candidates_by_order[order].append(child)
            raw_dirs_by_order[order].add(raw_dir)

    for order, candidates in candidates_by_order.items():
        keys = {merge_key_from_fingerprint(child.get("fingerprint", "")) for child in candidates}
        if "" in keys:
            bucket = (order, "")
            candidates_by_key_bucket[bucket].extend(candidates)
            raw_dirs_by_key_bucket[bucket].update(raw_dirs_by_order[order])
            continue
        for child in candidates:
            merge_key = merge_key_from_fingerprint(child.get("fingerprint", ""))
            bucket = (order, merge_key)
            candidates_by_key_bucket[bucket].append(child)
            raw_dirs_by_key_bucket[bucket].add(Path(child["_raw_dir"]))

    for (order, merge_key), candidates in candidates_by_key_bucket.items():
        detail_keys = {current_detail_key(child.get("detail_key", "")) for child in candidates}
        if len(candidates) >= detail_split_min and "" not in detail_keys and len(detail_keys) > 1:
            for child in candidates:
                bucket = (order, merge_key, current_detail_key(child.get("detail_key", "")))
                candidates_by_bucket[bucket].append(child)
                raw_dirs_by_bucket[bucket].add(Path(child["_raw_dir"]))
        else:
            bucket = (order, merge_key, "")
            candidates_by_bucket[bucket].extend(candidates)
            raw_dirs_by_bucket[bucket].update(raw_dirs_by_key_bucket[(order, merge_key)])

    reps = load_reps(run_dir)
    existing_by_order: dict[int, list[dict]] = defaultdict(list)
    for meta in reps.values():
        existing_by_order[int(meta["order"])].append(meta)

    tasks = [
        prepare_merge_task(
            run_dir,
            manifest,
            order,
            merge_key,
            detail_key,
            existing_by_order.get(order, []),
            candidates,
            raw_dirs_by_bucket[(order, merge_key, detail_key)],
        )
        for (order, merge_key, detail_key), candidates in sorted(candidates_by_bucket.items())
    ]
    workers = bounded_merge_workers(merge_workers, len(tasks), merge_reservation_gb, reserve_ram_gb)
    print(f"merging {len(tasks)} order/key/detail buckets with concurrency {workers}")
    for task in tasks:
        append_jsonl(
            run_dir / "jobs" / "running.jsonl",
            {
                "type": "merge_bucket",
                "order": task["order"],
                "merge_key_label": task["merge_key_label"],
                "detail_key_label": task["detail_key_label"],
                "candidate_count": task["candidate_count"],
                "existing_count": task["existing_count"],
                "merge_dir": str(task["merge_dir"]),
                "command": task["cmd"],
                "started_at": utc_now(),
                "workspace_cap": CLASS_LIMITS["medium"]["cap"],
                "wall_limit_seconds": CLASS_LIMITS["medium"]["seconds"],
            },
        )

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_task = {pool.submit(run_merge_task, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_to_task):
            result = future.result()
            results.append(result)
            print(json.dumps(result, sort_keys=True))

    committed = 0
    task_by_merge_dir = {str(task["merge_dir"]): task for task in tasks}
    for result in sorted(
        results,
        key=lambda item: (item["order"], item["merge_key_label"], item["detail_key_label"], item["merge_dir"]),
    ):
        task = task_by_merge_dir[result["merge_dir"]]
        if result["returncode"] != 0 or not result["sentinel"]:
            append_jsonl(
                run_dir / "jobs" / "failed.jsonl",
                {
                    "type": "merge_bucket",
                    "order": result["order"],
                    "merge_key_label": result["merge_key_label"],
                    "detail_key_label": result["detail_key_label"],
                    "candidate_count": result["candidate_count"],
                    "existing_count": result["existing_count"],
                    "returncode": result["returncode"],
                    "proposal_path": result["proposal_path"],
                    "merge_dir": result["merge_dir"],
                    "finished_at": utc_now(),
                },
            )
            failed_raw_dirs.update(task["raw_dirs"])
            continue
        append_jsonl(run_dir / "jobs" / "completed.jsonl", result)
        committed += commit_merge_proposals(run_dir, Path(result["proposal_path"]))
        cache_path = Path(result["cache_path"])
        if cache_path.exists():
            with cache_path.open() as src, (run_dir / "caches" / "conjugacy_tests.jsonl").open("a") as dst:
                shutil.copyfileobj(src, dst)
    for raw_dir in raw_jobs:
        if raw_dir not in failed_raw_dirs:
            atomic_write(raw_dir / "MERGED", "ok\n")
    return committed


def commit_merge_proposals(run_dir: Path, proposal_path: Path) -> int:
    proposals = read_jsonl(proposal_path)
    temp_to_rep: dict[str, int] = {}
    created = 0
    incidence_path = run_dir / "incidence.jsonl"
    for prop in proposals:
        if prop["kind"] == "new":
            rep_id = next_sequence(run_dir, "rep")
            temp_to_rep[prop["temp_id"]] = rep_id
            dest = rep_gap_path(run_dir, rep_id)
            shutil.copy2(prop["raw_path"], dest)
            meta = {
                "id": rep_id,
                "order": prop["order"],
                "rep_path": str(dest),
                "status": "queued",
                "job_class": "light",
                "top_class": None,
                "maximal_count": None,
                "parent_ids": [prop["parent_id"]],
                "fingerprint": prop.get("fingerprint", ""),
                "detail_key": current_detail_key(prop.get("detail_key", "")),
                "first_discovery_raw_id": prop["raw_id"],
                "created_at": utc_now(),
            }
            save_rep(run_dir, meta)
            append_jsonl(
                incidence_path,
                {
                    "parent_id": prop["parent_id"],
                    "child_id": rep_id,
                    "source": "merge_new",
                    "raw_id": prop["raw_id"],
                },
            )
            created += 1
        elif prop["kind"] == "identified":
            if prop["target_kind"] == "existing":
                target = prop["target_id"]
            else:
                target = temp_to_rep[prop["target_temp_id"]]
            append_jsonl(
                incidence_path,
                {
                    "parent_id": prop["parent_id"],
                    "child_id": target,
                    "source": "merge_identified",
                    "raw_id": prop["raw_id"],
                },
            )
    return created


def select_frontier(run_dir: Path, include_heavy: bool, max_jobs: int, only_top_light: bool) -> list[int]:
    reps = load_reps(run_dir)
    selected = []
    for rid, meta in sorted(reps.items()):
        if meta.get("status") != "queued":
            continue
        job_class = meta.get("job_class", "light")
        if job_class in {"heavy", "orthogonal_fallback"} and not include_heavy:
            continue
        if only_top_light and meta.get("top_class") not in [1, 2, 3, 4, 5, 6, 7, 10, 11]:
            continue
        selected.append(rid)
        if len(selected) >= max_jobs:
            break
    return selected


def cmd_run_round(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    manifest = load_manifest(run_dir)
    recover(run_dir)
    raw_backlog = unmerged_raw_jobs(run_dir)
    if raw_backlog:
        print(f"merging existing raw backlog before launching workers: {len(raw_backlog)} raw jobs")
        merge_count = merge_unmerged(
            run_dir,
            manifest,
            args.merge_workers,
            args.merge_reservation_gb,
            args.reserve_ram_gb,
            args.detail_split_min,
        )
        snapshot(run_dir, f"run_round_backlog_merge_new_{merge_count}")
        return
    jobs = select_frontier(run_dir, args.include_heavy, args.max_jobs, args.only_top_light)
    if not jobs:
        print("no eligible queued representatives")
        merge_count = merge_unmerged(
            run_dir,
            manifest,
            args.merge_workers,
            args.merge_reservation_gb,
            args.reserve_ram_gb,
            args.detail_split_min,
        )
        snapshot(run_dir, f"run_round_merge_only_new_{merge_count}")
        return
    workers = min(args.workers, len(jobs))
    print(f"launching {len(jobs)} worker jobs with concurrency {workers}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_worker, run_dir, manifest, rid) for rid in jobs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            print(json.dumps(result, sort_keys=True))
    new_count = merge_unmerged(
        run_dir,
        manifest,
        args.merge_workers,
        args.merge_reservation_gb,
        args.reserve_ram_gb,
        args.detail_split_min,
    )
    snap = snapshot(run_dir, f"run_round_new_{new_count}")
    print(f"committed {new_count} new representatives; snapshot {snap.name}")


def recover(run_dir: Path) -> None:
    for tmp in run_dir.rglob("*.tmp"):
        tmp.unlink()
    reps = load_reps(run_dir)
    changed = False
    for meta in reps.values():
        if meta.get("status") == "processing":
            meta["status"] = "queued"
            meta["recovered_at"] = utc_now()
            save_rep(run_dir, meta)
            changed = True
    if changed:
        snapshot(run_dir, "recover")


def cmd_recover(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    recover(run_dir)
    print(f"recovered {run_dir}")


def status_data(run_dir: Path) -> dict:
    reps = load_reps(run_dir)
    counts = Counter(meta.get("status", "unknown") for meta in reps.values())
    classes = Counter(meta.get("job_class", "unknown") for meta in reps.values())
    orders = Counter(str(meta.get("order")) for meta in reps.values())
    latest = run_dir / "snapshots" / "latest"
    latest_snapshot = latest.read_text().strip() if latest.exists() else None
    return {
        "run_dir": str(run_dir),
        "representative_count": len(reps),
        "status_counts": dict(counts),
        "job_class_counts": dict(classes),
        "order_counts": dict(orders),
        "unmerged_raw_jobs": len(unmerged_raw_jobs(run_dir)),
        "latest_snapshot": latest_snapshot,
    }


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(status_data(Path(args.run_dir).resolve()), indent=2, sort_keys=True))


def cmd_validate(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    manifest = load_manifest(run_dir)
    out = run_dir / "validation" / f"sp{args.dim}_validation.json"
    expr = f"SCRIPT_DIR:={gap_string(SCRIPT_DIR)};;DIM:={args.dim};;VALIDATION_PATH:={gap_string(out)};;"
    cmd = [
        manifest["gap"],
        "-q",
        "--quitonbreak",
        "-K",
        args.cap,
        "-c",
        expr,
        str(SCRIPT_DIR / "sp8_validate.g"),
    ]
    subprocess.run(cmd, check=True)
    print(out.read_text().strip())


def cmd_export(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    reps = load_reps(run_dir)
    out = Path(args.output) if args.output else run_dir / "export" / "reps.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "order",
                "status",
                "job_class",
                "top_class",
                "maximal_count",
                "rep_path",
                "fingerprint",
                "detail_key",
            ],
        )
        writer.writeheader()
        for rid in sorted(reps):
            meta = reps[rid]
            writer.writerow({field: meta.get(field) for field in writer.fieldnames})
    print(out)


def cmd_run(args: argparse.Namespace) -> None:
    for i in range(args.rounds):
        print(f"round {i + 1}/{args.rounds}")
        args.max_jobs = args.max_jobs
        cmd_run_round(args)
        data = status_data(Path(args.run_dir).resolve())
        if data["status_counts"].get("queued", 0) == 0 and data["unmerged_raw_jobs"] == 0:
            print("frontier exhausted")
            break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight")
    p.add_argument("--gap", default=str(DEFAULT_GAP))
    p.add_argument("--reduced", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("init")
    p.add_argument("run_dir")
    p.add_argument("--gap", default=str(DEFAULT_GAP))
    p.add_argument("--dim", type=int, default=8)
    p.add_argument("--ordinary-workers", type=int, default=24)
    p.add_argument("--heavy-workers", type=int, default=1)
    p.add_argument("--reduced", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run-round")
    p.add_argument("run_dir")
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--max-jobs", type=int, default=24)
    p.add_argument("--merge-workers", type=int, default=8)
    p.add_argument("--merge-reservation-gb", type=float, default=4.0)
    p.add_argument("--reserve-ram-gb", type=float, default=8.0)
    p.add_argument("--detail-split-min", type=int, default=256)
    p.add_argument("--include-heavy", action="store_true")
    p.add_argument("--only-top-light", action="store_true")
    p.set_defaults(func=cmd_run_round)

    p = sub.add_parser("run")
    p.add_argument("run_dir")
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--max-jobs", type=int, default=24)
    p.add_argument("--merge-workers", type=int, default=8)
    p.add_argument("--merge-reservation-gb", type=float, default=4.0)
    p.add_argument("--reserve-ram-gb", type=float, default=8.0)
    p.add_argument("--detail-split-min", type=int, default=256)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--include-heavy", action="store_true")
    p.add_argument("--only-top-light", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("recover")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("status")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("validate")
    p.add_argument("run_dir")
    p.add_argument("--dim", type=int, default=6)
    p.add_argument("--cap", default="4g")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("export")
    p.add_argument("run_dir")
    p.add_argument("--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backfill-details")
    p.add_argument("run_dir")
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--chunk-size", type=int, default=200)
    p.add_argument("--cap", default="2g")
    p.add_argument("--seconds", type=int, default=60 * 60)
    p.add_argument("--reservation-gb", type=float, default=1.0)
    p.add_argument("--reserve-ram-gb", type=float, default=8.0)
    p.add_argument("--max-order", type=int, default=4096)
    p.add_argument("--orders", help="comma-separated order filter, e.g. 1024,256")
    p.add_argument("--skip-raw", action="store_true")
    p.add_argument("--include-merged-raw", action="store_true")
    p.add_argument("--force-live", action="store_true")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_backfill_details)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
