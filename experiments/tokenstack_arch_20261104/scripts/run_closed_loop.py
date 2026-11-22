#!/usr/bin/env python3
"""Run real Ramulator2 closed-loop KV migrations and audit raw command/events."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml


OPS = {"PIM_MAC_AB": 4, "PIM_MAC_SB": 5, "PIM_MAC_PB": 6, "PIM_WR_GB": 7,
       "PIM_MV_SB": 8, "PIM_MV_GB": 9, "PIM_SFM": 10, "PIM_SET_MODEL": 11,
       "PIM_SET_HEAD": 12, "PIM_BARRIER": 13}
COMMANDS = {0: "RD", 1: "WR", 4: "MACAB", 5: "MACSB", 6: "MACPB", 7: "WRGB",
            8: "MVSB", 9: "MVGB", 10: "SFM", 11: "SETM", 12: "SETH"}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def records(path: Path, strings: set[str]) -> list[dict]:
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    return [{k: v if k in strings else int(v) for k, v in row.items()} for row in rows]


def decode(address: int, config: dict) -> tuple[int, ...]:
    """Independent decoder for the explicitly supported smoke-test geometry."""
    dram = config["MemorySystem"]["DRAM"]
    check(dram["org"] == {"preset": "HBM3_8Gb_2R", "channel": 1}, "validator requires known geometry")
    address //= config["Frontend"]["burst_bytes"]
    result = []
    for count in reversed([1, 2, 2, 4, 4, 16384, 32]):
        result.append(address % count)
        address //= count
    check(address == 0, "address aliases after decode")
    return tuple(reversed(result))


def validate(run: Path, config: dict, workload: dict, stdout: str) -> dict:
    requests = records(run / "requests.csv", {"job_id", "phase", "event"})
    jobs = records(run / "jobs.csv", {"job_id", "kind"})
    resources = records(run / "resources.csv", {"job_id", "resource"})
    stats = yaml.safe_load(stdout[stdout.index("Frontend:"):])
    memory = stats["MemorySystem"]
    frontend = stats["Frontend"]
    f = config["Frontend"]
    burst = f["burst_bytes"]
    tck = frontend["tokenstack_tck_ps"]
    stop = memory["memory_system_cycles"]
    check(tck == 769, "unexpected smoke-test clock")
    check(all(r["schema_version"] == 1 for r in requests + jobs + resources), "schema mismatch")
    check(Counter(r["request_id"] for r in requests) == Counter(range(len(requests))), "request IDs dropped/duplicated")
    expected_jobs = {j["id"]: j for j in workload["jobs"]}
    check(Counter(j["job_id"] for j in jobs) == Counter(expected_jobs.keys()), "jobs dropped/duplicated")
    check({r["job_id"] for r in requests} == set(expected_jobs), "unexpected request jobs")
    check({r["job_id"] for r in resources} == set(expected_jobs), "unexpected resource jobs")
    by_id = {j["job_id"]: j for j in jobs}
    read_total = write_total = logical_total = 0
    for job_id, source in expected_jobs.items():
        job = by_id[job_id]
        check(job["kind"] == source["kind"], "kind mismatch")
        check(job["issue_cycle"] == source["issue_cycle"], "issue mismatch")
        check(job["issue_cycle"] <= job["eligible_cycle"] <= job["admit_cycle"] <= job["start_cycle"] < job["complete_cycle"] <= stop,
              "invalid job boundaries")
        for dep in source.get("depends_on", []):
            check(by_id[dep]["complete_cycle"] <= job["eligible_cycle"], "dependency violated")
        selected = [r for r in requests if r["job_id"] == job_id]
        for row in selected:
            check(job["start_cycle"] <= row["accept_cycle"] <= row["command_cycle"] <= row["event_cycle"]
                  == row["callback_cycle"] <= job["complete_cycle"], "callback time conservation")
            check(row["channel"] == decode(row["address"], config)[0] == 0, "channel mismatch")
        check(job["complete_cycle"] == max(r["event_cycle"] for r in selected), "job horizon mismatch")
        if source["kind"] == "attention":
            check(len(selected) == len(source["requests"]), "PIM request count mismatch")
            for row in selected:
                operation = source["requests"][row["index"]]
                check(row["phase"] == "attention" and row["type_id"] == OPS[operation["op"]]
                      and row["address"] == f["compute_base"] + operation["offset"] and row["bytes"] == 0,
                      "PIM request changed")
                check(row["event"] == ("barrier_removed" if row["type_id"] == 13 else "pim_final_command_issued"), "PIM event mislabeled")
            check(Counter(r["index"] for r in selected) == Counter(range(len(source["requests"]))), "PIM indices lost")
            check(job["logical_bytes"] == job["read_bytes"] == job["write_bytes"] == 0, "invented PIM bytes")
            continue
        logical = source["logical_bytes"]
        compressed = logical * f["compressed_num"] // f["compressed_den"]
        promotion = source["kind"] == "promotion"
        read_bytes, write_bytes = (compressed, logical) if promotion else (logical, compressed)
        check((job["logical_bytes"], job["read_bytes"], job["write_bytes"]) == (logical, read_bytes, write_bytes), "migration byte mismatch")
        logical_total += logical
        read_total += read_bytes
        write_total += write_bytes
        for phase, count_bytes, base, type_id in (
            ("read", read_bytes, f["capacity_base"] if promotion else f["compute_base"], 0),
            ("write", write_bytes, f["compute_base"] if promotion else f["capacity_base"], 1),
        ):
            phase_rows = [r for r in selected if r["phase"] == phase]
            check(Counter(r["index"] for r in phase_rows) == Counter(range(count_bytes // burst)), "burst indices lost")
            check(sum(r["bytes"] for r in phase_rows) == count_bytes, "physical bytes lost")
            for row in phase_rows:
                check(row["type_id"] == type_id and row["bytes"] == burst
                      and row["address"] == base + source["offset"] + row["index"] * burst, "burst routing mismatch")
                latency = frontend["tokenstack_read_latency_cycles" if phase == "read" else "tokenstack_write_recovery_cycles"]
                check(row["event_cycle"] - row["command_cycle"] == latency, "DRAM latency mismatch")
                check(row["event"] == ("read_data_ready" if phase == "read" else "write_recovery_done"), "DRAM event mislabeled")
        link = sorted((r for r in resources if r["job_id"] == job_id and r["resource"] != "channel_base_die_transaction"),
                      key=lambda r: r["start_cycle"])
        check([r["resource"] for r in link] == ["source_tsv_base_die", "target_tsv_base_die"], "link stages missing")
        check([r["bytes"] for r in link] == [read_bytes, write_bytes], "link byte conservation")
        check(link[0]["start_cycle"] == max(r["event_cycle"] for r in selected if r["phase"] == "read"), "source transfer before read data")
        check(link[0]["end_cycle"] == link[1]["start_cycle"], "link gap/overlap")
        writes = [r for r in selected if r["phase"] == "write"]
        check(link[1]["start_cycle"] == min(r["accept_cycle"] for r in writes), "early DMA descriptors not exercised")
        check(all(r["command_cycle"] > link[1]["end_cycle"] for r in writes), "WR issued before transfer completed")
        for interval in link:
            expected = math.ceil(interval["bytes"] * 1000 / (f["base_die_link_GBps"] * tck))
            check(interval["end_cycle"] - interval["start_cycle"] == expected, "bandwidth/time conversion")

    transactions = sorted((r for r in resources if r["resource"] == "channel_base_die_transaction"), key=lambda r: r["start_cycle"])
    check(len(transactions) == len(jobs), "missing transaction reservations")
    for row in transactions:
        job = by_id[row["job_id"]]
        check((row["start_cycle"], row["end_cycle"]) == (job["start_cycle"], job["complete_cycle"]), "resource horizon mismatch")
    check(all(a["end_cycle"] <= b["start_cycle"] for a, b in zip(transactions, transactions[1:])), "shared resources overlap")
    check(frontend["tokenstack_buffer_peak_bytes"] == max(j["logical_bytes"] for j in jobs) <= f["buffer_bytes"], "buffer overflow/leak")
    check(frontend["tokenstack_queue_peak_jobs"] <= f["queue_depth"], "queue overflow")
    check(memory["total_num_read_requests"] * burst == read_total and memory["total_num_write_requests"] * burst == write_total,
          "controller accepted bytes differ")
    check(stop == max(j["complete_cycle"] for j in jobs) == max(r["event_cycle"] for r in requests), "simulation stopped before/after drain")

    raw_commands = []
    for line in (run / "commands.log.ch0").read_text().splitlines():
        fields = [v.strip() for v in line.split(",")]
        raw_commands.append((int(fields[0]), fields[1], tuple(map(int, fields[2:]))))
    final_commands = Counter(c for c in raw_commands if c[1] in COMMANDS.values())
    from_callbacks = Counter((r["command_cycle"], COMMANDS[r["type_id"]], decode(r["address"], config))
                             for r in requests if r["type_id"] != 13)
    check(final_commands == from_callbacks, "raw issued commands differ from callbacks")
    links = [r for r in resources if r["resource"] != "channel_base_die_transaction"]
    for interval in links:
        check(not any(interval["start_cycle"] < c[0] <= interval["end_cycle"] for c in raw_commands), "controller issued command during link gate")
    blocked = sum(r["end_cycle"] - r["start_cycle"] for r in links)
    check(frontend["tokenstack_controller_block_cycles"] == blocked, "controller did not honor all gate cycles")
    return dict(stop_cycle=stop, stop_ns=stop * tck / 1000, tck_ps=tck, requests=len(requests),
                pim_requests=sum(r["phase"] == "attention" for r in requests),
                migration_read_requests=read_total // burst, migration_write_requests=write_total // burst,
                logical_migration_bytes=logical_total, physical_read_bytes=read_total, physical_write_bytes=write_total,
                explicit_link_block_cycles=blocked, buffer_peak_bytes=frontend["tokenstack_buffer_peak_bytes"],
                refresh_commands=sum(c[1] in ("REFab", "REFsb") for c in raw_commands),
                queue_peak_jobs=frontend["tokenstack_queue_peak_jobs"], admission_block_cycles=frontend["tokenstack_admission_block_cycles"],
                controller_send_retries=frontend["tokenstack_send_retries"],
                job_order=[r["job_id"] for r in transactions], job_completion_cycles={j["job_id"]: j["complete_cycle"] for j in jobs},
                request_byte_command_time_conservation=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run_id must be a new simple directory name")
    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[1]
    repo = workspace / "attacc_simulator/ramulator2"
    binary = repo / "build/ramulator2"
    output = root / "results" / args.run_id
    output.mkdir(exist_ok=False)
    source_dir = output / "sources"
    source_dir.mkdir()
    for relative in ("src/frontend/impl/memory_trace/tokenstack_trace.cpp", "src/frontend/CMakeLists.txt",
                     "src/dram_controller/tokenstack_controller.h", "src/dram_controller/impl/hbm3_pim_controller.cpp",
                     "src/dram_controller/impl/scheduler/pim_scheduler.cpp"):
        target = source_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relative, target)
    shutil.copy2(Path(__file__), source_dir / Path(__file__).name)
    shutil.copy2(root / "config/tokenstack_closed_loop.yaml", source_dir / "tokenstack_closed_loop.yaml")
    shutil.copy2(root / "traces/closed_loop_kv_v1.yaml", source_dir / "closed_loop_kv_v1.yaml")
    shutil.copy2(root / "tests/test_closed_loop.py", source_dir / "test_closed_loop.py")
    shutil.copy2(root / "README.md", source_dir / "README.md")
    shutil.copy2(repo / "build/CMakeCache.txt", output / "CMakeCache.txt")
    shutil.copy2(repo / "build/compile_commands.json", output / "compile_commands.json")
    for name in ("ramulator-retirement-v1.patch", "tokenstack-closed-loop-v1.patch"):
        shutil.copy2(root / "patches" / name, source_dir / name)
    cache = (repo / "build/CMakeCache.txt").read_text()
    compiler = re.search(r"^CMAKE_CXX_COMPILER:[^=]+=(.+)$", cache, re.MULTILINE)
    if compiler is None:
        raise RuntimeError("missing configured CXX compiler in CMake cache")
    metadata = dict(argv=sys.argv, python=sys.version, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    library_sha256=hashlib.sha256((repo / "libramulator.so").read_bytes()).hexdigest(),
                    configured_compiler=compiler[1],
                    compiler_version=subprocess.check_output([compiler[1], "--version"], text=True).strip(),
                    cmake_version=subprocess.check_output(["cmake", "--version"], text=True).strip(),
                    ramulator_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                    attacc_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo.parent, text=True).strip(),
                    build_command="cmake --build attacc_simulator/ramulator2/build -j2", seed=None,
                    note="deterministic; behavioral nonpreemptive Base Die plus real AttAcc DRAM commands")
    build_log = Path("/tmp/tokenstack-closed-loop-build-v2.log")
    if build_log.exists():
        shutil.copy2(build_log, output / "build.log")
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "ramulator.status.txt").write_text(subprocess.check_output(["git", "status", "--short"], cwd=repo, text=True))
    config = yaml.safe_load((root / "config/tokenstack_closed_loop.yaml").read_text())
    workload = yaml.safe_load((root / "traces/closed_loop_kv_v1.yaml").read_text())
    baseline = copy.deepcopy(workload)
    baseline["jobs"] = [j for j in baseline["jobs"] if j["kind"] == "attention"]
    kept = {j["id"] for j in baseline["jobs"]}
    for job in baseline["jobs"]:
        job["depends_on"] = [d for d in job.get("depends_on", []) if d in kept]
    reports = {}
    for name, trace in (("no-migration", baseline), ("with-migration", workload), ("slow-link", workload),
                        ("small-queue", workload), ("refresh-stress", workload)):
        run = output / name
        run.mkdir()
        effective = copy.deepcopy(config)
        f = effective["Frontend"]
        if name == "slow-link":
            f["base_die_link_GBps"] /= 32
        if name == "refresh-stress":
            f["base_die_link_GBps"] /= 256
        if name == "small-queue":
            f["queue_depth"] = 1
        for key, filename in (("path", "workload.yaml"), ("request_log", "requests.csv"), ("job_log", "jobs.csv"), ("resource_log", "resources.csv")):
            f[key] = str(run / filename)
        effective["MemorySystem"]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(run / "commands.log")
        (run / "workload.yaml").write_text(yaml.safe_dump(trace, sort_keys=False))
        path = run / "ramulator.yaml"
        path.write_text(yaml.safe_dump(effective, sort_keys=False))
        command = [str(binary), "-f", str(path)]
        (run / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        process = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=60)
        (run / "stdout.log").write_text(process.stdout)
        (run / "stderr.log").write_text(process.stderr)
        process.check_returncode()
        reports[name] = validate(run, effective, trace, process.stdout)
    normal = reports["with-migration"]
    check(normal["job_order"] == ["promotion-0", "attention-0", "attention-1", "demotion-0"], "priority/dependency order")
    check(normal["physical_read_bytes"] == normal["physical_write_bytes"] == 11264, "small KV totals")
    check(normal["pim_requests"] == reports["no-migration"]["pim_requests"] == 12, "foreground requests changed")
    check(normal["job_completion_cycles"]["attention-0"] > reports["no-migration"]["job_completion_cycles"]["attention-0"], "no runtime foreground contention")
    check(reports["slow-link"]["stop_cycle"] > normal["stop_cycle"], "bandwidth did not feed back into controller timeline")
    check(reports["small-queue"]["admission_block_cycles"] > 0, "finite queue backpressure not exercised")
    check(reports["refresh-stress"]["refresh_commands"] > 0, "refresh enqueue/drain not exercised")
    summary = dict(schema_version=1, model="single-channel conservative closed-loop; disjoint logical regions, shared DRAM timing domain",
                   completion_semantics="PIM final command issue/barrier removal; RD data return; WR conservative recovery boundary",
                   assumptions="serial extra internal link, no overlap, throughput-matched quantization, no metadata", scenarios=reports)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
