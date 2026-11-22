#!/usr/bin/env python3
"""Audit two real DRAM timing domains behind one TokenStack resource arbiter."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys

import yaml

from run_closed_loop import COMMANDS, OPS, check, records

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1] / "attacc_simulator/ramulator2"


def no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def layer_of(address: int, config: dict) -> tuple[int, int]:
    memory = config["MemorySystem"]
    for layer, key in enumerate(("capacity_base", "compute_base")):
        local = address - memory[key]
        if 0 <= local < memory["region_bytes"]:
            return layer, local
    raise AssertionError("global address outside layer map")


def decode(local: int) -> tuple[int, ...]:
    check(local >= 0 and local % 32 == 0, "unaligned local address")
    local //= 32
    result = []
    for count in reversed([1, 2, 2, 4, 4, 16384, 32]):
        result.append(local % count)
        local //= count
    check(local == 0, "layer-local alias")
    return tuple(reversed(result))


def read_commands(run: Path) -> list[tuple]:
    result = []
    for layer, name in enumerate(("capacity", "compute")):
        for line in (run / f"{name}.commands.ch0").read_text().splitlines():
            fields = [v.strip() for v in line.split(",")]
            result.append((layer, int(fields[0]), fields[1], tuple(map(int, fields[2:]))))
    return result


def expected_latency(config: dict, layer: int, type_id: int) -> int:
    timing = config["MemorySystem"][("Capacity", "Compute")[layer]]["DRAM"]["timing"]
    check(timing["preset"] == "HBM3_5.2Gbps", "validator requires known timing preset")
    if type_id == 0:
        return timing.get("nCL", 19) + timing.get("nBL", 2)
    if type_id == 1:
        return timing.get("nCWL", 6) + timing.get("nBL", 2) + timing.get("nWR", 21)
    return 0


def validate_layered(run: Path, config: dict, workload: dict, stdout: str) -> dict:
    f, mem = config["Frontend"], config["MemorySystem"]
    for key in ("Capacity", "Compute"):
        check(mem[key]["DRAM"]["org"] == {"preset": "HBM3_8Gb_2R", "channel": 1}, "unknown validation geometry")
    requests = records(run / "requests.csv", {"job_id", "phase", "event"})
    jobs = records(run / "jobs.csv", {"job_id", "kind"})
    resources = records(run / "resources.csv", {"job_id", "resource"})
    grants = records(run / "grants.csv", set())
    stats = yaml.safe_load(stdout[stdout.index("Frontend:"):])
    ms, fs = stats["MemorySystem"], stats["Frontend"]
    stop = ms["memory_system_cycles"]
    check(ms["stack_tck_ps"] == fs["tokenstack_tck_ps"] == 769, "clock mismatch")
    check(all(r["schema_version"] == 2 and r["tck_ps"] == 769 for r in requests), "request schema/clock mismatch")
    check(all(r["schema_version"] == 1 for r in jobs + resources + grants), "event schema mismatch")
    check(Counter(r["request_id"] for r in requests) == Counter(range(len(requests))), "request ID loss/duplication")
    inputs = {j["id"]: j for j in workload["jobs"]}
    check(Counter(j["job_id"] for j in jobs) == Counter(inputs.keys()), "job loss/duplication")
    by_id = {j["job_id"]: j for j in jobs}
    check({r["job_id"] for r in requests} == set(inputs), "unexpected request job")
    logical_total = migration_read = migration_write = 0
    for job_id, source in inputs.items():
        job = by_id[job_id]
        rows = [r for r in requests if r["job_id"] == job_id]
        check(job["kind"] == source["kind"] and job["issue_cycle"] == source["issue_cycle"], "job trigger mismatch")
        check(job["issue_cycle"] <= job["eligible_cycle"] <= job["admit_cycle"] <= job["start_cycle"] < job["complete_cycle"] <= stop,
              "job time order")
        for dep in source.get("depends_on", []):
            check(by_id[dep]["complete_cycle"] <= job["eligible_cycle"], "dependency violation")
        expected = []
        if source["kind"] in ("attention", "access"):
            layer_name = source.get("layer", "compute")
            for index, op in enumerate(source["requests"]):
                type_id = OPS[op["op"]] if source["kind"] == "attention" else {"LD": 0, "ST": 1}[op["op"]]
                expected.append((source["kind"], index, type_id, mem[f"{layer_name}_base"] + op["offset"], 0 if type_id >= 4 else 32))
            check(job["logical_bytes"] == job["read_bytes"] == job["write_bytes"] == 0, "invented logical bytes")
        else:
            logical = source["logical_bytes"]
            compressed = logical * f["compressed_num"] // f["compressed_den"]
            promotion = source["kind"] == "promotion"
            rb, wb = (compressed, logical) if promotion else (logical, compressed)
            check((job["logical_bytes"], job["read_bytes"], job["write_bytes"]) == (logical, rb, wb), "migration byte mismatch")
            logical_total += logical; migration_read += rb; migration_write += wb
            for phase, count_bytes, layer_name, type_id in (
                ("read", rb, "capacity" if promotion else "compute", 0),
                ("write", wb, "compute" if promotion else "capacity", 1),
            ):
                for index in range(count_bytes // 32):
                    expected.append((phase, index, type_id, mem[f"{layer_name}_base"] + source["offset"] + index * 32, 32))
            links = sorted((r for r in resources if r["job_id"] == job_id and r["resource"] != "channel_base_die_transaction"),
                           key=lambda r: r["start_cycle"])
            check([r["resource"] for r in links] == ["source_tsv_base_die", "target_tsv_base_die"], "missing link stages")
            check([r["bytes"] for r in links] == [rb, wb], "link byte mismatch")
            check(links[0]["start_cycle"] == max(r["event_cycle"] for r in rows if r["phase"] == "read"), "link before source ready")
            check(links[0]["end_cycle"] == links[1]["start_cycle"], "link gap/overlap")
            writes = [r for r in rows if r["phase"] == "write"]
            check(min(r["accept_cycle"] for r in writes) == links[1]["start_cycle"], "early write descriptor missing")
            check(all(r["command_cycle"] > links[1]["end_cycle"] for r in writes), "early target WR")
            for link in links:
                check(link["end_cycle"] - link["start_cycle"] == math.ceil(link["bytes"] * 1000 / (f["base_die_link_GBps"] * 769)), "link cycle conversion")
        actual = [(r["phase"], r["index"], r["type_id"], r["address"], r["bytes"]) for r in rows]
        check(Counter(actual) == Counter(expected), "request count/type/address/bytes mismatch")
        check(job["complete_cycle"] == max(r["event_cycle"] for r in rows), "job completion lost")
        for row in rows:
            layer, local = layer_of(row["address"], config)
            check((row["layer_id"], row["local_address"], row["channel"]) == (layer, local, 0), "layer routing mismatch")
            check(layer == 1 or row["type_id"] in (0, 1), "PIM leaked into Capacity")
            check(job["start_cycle"] <= row["accept_cycle"] <= row["command_cycle"] <= row["event_cycle"]
                  == row["callback_cycle"] <= job["complete_cycle"], "request time conservation")
            check(row["event_cycle"] - row["command_cycle"] == expected_latency(config, layer, row["type_id"]), "layer-local latency mismatch")
            event = "read_data_ready" if row["type_id"] == 0 else "write_recovery_done" if row["type_id"] == 1 else "barrier_removed" if row["type_id"] == 13 else "pim_final_command_issued"
            check(row["event"] == event, "completion semantics changed")

    commands = read_commands(run)
    actual_final = Counter(c for c in commands if c[2] in COMMANDS.values())
    expected_final = Counter((r["layer_id"], r["command_cycle"], COMMANDS[r["type_id"]], decode(r["local_address"]))
                             for r in requests if r["type_id"] != 13)
    check(actual_final == expected_final, "layer command logs differ from callbacks")
    check([g["cycle"] for g in grants] == list(range(1, stop + 1)), "grant clock lost/duplicated")
    previous = None
    for grant in grants:
        owner = grant["owner_layer"]
        check(owner in (-1, 0, 1), "invalid grant")
        if grant["external_blocked"]:
            check(owner == -1, "grant during TSV block")
        if owner >= 0:
            check(grant[("capacity_pending", "compute_pending")[owner]] == 1, "granted empty layer")
            if previous and previous["owner_layer"] != owner:
                check(grant["cycle"] - previous["cycle"] > mem["domain_switch_guard_cycles"], "shared bus switch guard bypass")
            previous = grant
    for layer, cycle, _, _ in commands:
        check(grants[cycle - 1]["owner_layer"] == layer and not grants[cycle - 1]["external_blocked"], "ungranted layer issued command")
    links = [r for r in resources if r["resource"] != "channel_base_die_transaction"]
    blocked = sum(r["end_cycle"] - r["start_cycle"] for r in links)
    for grant in grants:
        check(bool(grant["external_blocked"]) == any(r["start_cycle"] < grant["cycle"] <= r["end_cycle"] for r in links), "link gate interval mismatch")
    check(blocked == sum(g["external_blocked"] for g in grants) == ms["stack_external_block_cycles"]
          == fs["tokenstack_controller_block_cycles"], "shared gate counted per layer or lost")
    transactions = sorted((r for r in resources if r["resource"] == "channel_base_die_transaction"), key=lambda r: r["start_cycle"])
    check(Counter(t["job_id"] for t in transactions) == Counter(inputs.keys()), "missing transaction reservations")
    for tx in transactions:
        job = by_id[tx["job_id"]]
        check((tx["start_cycle"], tx["end_cycle"]) == (job["start_cycle"], job["complete_cycle"]), "reservation time mismatch")
    check(all(a["end_cycle"] <= b["start_cycle"] for a, b in zip(transactions, transactions[1:])), "shared transaction overlap")
    check(fs["tokenstack_buffer_peak_bytes"] == max(j["logical_bytes"] for j in jobs) <= f["buffer_bytes"], "buffer overflow/leak")
    check(fs["tokenstack_queue_peak_jobs"] <= f["queue_depth"], "queue overflow")
    totals = {}
    for layer, name in enumerate(("capacity", "compute")):
        counts = Counter(r["type_id"] for r in requests if r["layer_id"] == layer)
        totals[name] = dict(reads=counts[0], writes=counts[1], pim=sum(counts[t] for t in range(4, 14)))
        check(ms[f"{name}_read_requests"] == counts[0] and ms[f"{name}_write_requests"] == counts[1], "layer accepted counters mismatch")
        check(ms["capacity_pim_requests" if layer == 0 else "compute_pim_requests_including_barriers"] == totals[name]["pim"], "PIM counter mismatch")
        leaf = stats["LayerStats"][name]
        check(leaf["memory_system_cycles"] == stop and leaf["total_num_read_requests"] == counts[0]
              and leaf["total_num_write_requests"] == counts[1], "backend clock/counters diverged")
    check(ms["total_num_read_requests"] == sum(r["type_id"] == 0 for r in requests)
          and ms["total_num_write_requests"] == sum(r["type_id"] == 1 for r in requests), "Stack counts not conserved")
    request_horizon = max(r["event_cycle"] for r in requests)
    check(stop == max(request_horizon, max(c[1] for c in commands)), "simulator drain horizon mismatch")
    return dict(stop_cycle=stop, workload_event_horizon=request_horizon, tck_ps=769, requests=len(requests),
                logical_migration_bytes=logical_total, physical_migration_read_bytes=migration_read,
                physical_migration_write_bytes=migration_write, per_layer=totals,
                shared_link_block_cycles=blocked, domain_switch_stall_cycles=ms["stack_domain_switch_stall_cycles"],
                refresh_commands=sum(c[2] in ("REFab", "REFsb") for c in commands),
                job_order=[j["job_id"] for j in transactions], conservation_passed=True)


def run_case(run: Path, config: dict, workload: dict) -> tuple[dict, dict, subprocess.CompletedProcess]:
    run.mkdir()
    config = copy.deepcopy(config)
    for key, filename in (("path", "workload.yaml"), ("request_log", "requests.csv"),
                          ("job_log", "jobs.csv"), ("resource_log", "resources.csv")):
        config["Frontend"][key] = str(run / filename)
    config["MemorySystem"]["grant_log"] = str(run / "grants.csv")
    for key in ("Capacity", "Compute"):
        config["MemorySystem"][key]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(run / f"{key.lower()}.commands")
    (run / "workload.yaml").write_text(yaml.safe_dump(workload, sort_keys=False))
    (run / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    command = [str(REPO / "build/ramulator2"), "-f", str(run / "config.yaml")]
    (run / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    process = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=60, preexec_fn=no_core)
    (run / "stdout.log").write_text(process.stdout)
    (run / "stderr.log").write_text(process.stderr)
    return config, workload, process


def verify_isolation(run: Path) -> dict:
    commands = read_commands(run)
    cap = [c for c in commands if c[0] == 0]
    comp = [c for c in commands if c[0] == 1]
    check(Counter(c[3][5] for c in cap if c[2] == "ACT") == Counter([0, 1]), "Capacity row hit/conflict state not isolated")
    check(sum(c[2] == "PRE" for c in cap) == 1, "Capacity expected one row conflict")
    check(not any(c[2] in ("ACTAB", "MACAB", "PREA") for c in cap), "Compute broadcast modified Capacity")
    check(sum(c[2] == "ACT" for c in comp) == 1, "Compute cold/hit row state aliased")
    check(any(c[2] == "ACTAB" and c[3][5] == 2 for c in comp), "Compute PIM activation not exercised")
    return dict(capacity_cold_activations=2, capacity_row_conflicts=1,
                compute_normal_activations=1, compute_pim_broadcast_isolated=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("use a new simple directory name")
    output = ROOT / "results" / args.run_id
    output.mkdir(exist_ok=False)
    sources = output / "sources"
    sources.mkdir()
    for relative in ("src/frontend/impl/memory_trace/tokenstack_trace.cpp", "src/memory_system/impl/tokenstack_memory.cpp",
                     "src/memory_system/tokenstack_memory.h", "src/memory_system/CMakeLists.txt",
                     "src/memory_system/impl/PIM_DRAM_system.cpp", "src/dram_controller/tokenstack_controller.h",
                     "src/dram_controller/impl/hbm3_pim_controller.cpp", "src/addr_mapper/impl/hbm3_pim_linear_mappers.cpp"):
        path = sources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, path)
    for relative in ("scripts/run_layered.py", "scripts/run_closed_loop.py", "tests/test_layered.py", "config/tokenstack_layered.yaml",
                     "traces/closed_loop_kv_v1.yaml", "traces/layer_isolation_v1.yaml", "LAYERED.md"):
        path = sources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, path)
    for patch in (ROOT / "patches").glob("*.patch"):
        shutil.copy2(patch, sources / patch.name)
    for name in ("CMakeCache.txt", "compile_commands.json"):
        shutil.copy2(REPO / "build" / name, output / name)
    compiler = re.search(r"^CMAKE_CXX_COMPILER:[^=]+=(.+)$", (REPO / "build/CMakeCache.txt").read_text(), re.MULTILINE)[1]
    metadata = dict(argv=sys.argv, python=sys.version, seed=None, configured_compiler=compiler,
                    compiler_version=subprocess.check_output([compiler, "--version"], text=True),
                    ramulator_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                    attacc_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO.parent, text=True).strip(),
                    executable_sha256=hashlib.sha256((REPO / "build/ramulator2").read_bytes()).hexdigest(),
                    library_sha256=hashlib.sha256((REPO / "libramulator.so").read_bytes()).hexdigest(),
                    build_command="cmake --build attacc_simulator/ramulator2/build -j2")
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "git-status.txt").write_text(subprocess.check_output(["git", "status", "--short"], cwd=REPO, text=True))
    if Path("/tmp/tokenstack-layer-build-v2.log").exists():
        shutil.copy2("/tmp/tokenstack-layer-build-v2.log", output / "build.log")
    config = yaml.safe_load((ROOT / "config/tokenstack_layered.yaml").read_text())
    workload = yaml.safe_load((ROOT / "traces/closed_loop_kv_v1.yaml").read_text())
    isolation = yaml.safe_load((ROOT / "traces/layer_isolation_v1.yaml").read_text())
    baseline = copy.deepcopy(workload)
    baseline["jobs"] = [j for j in baseline["jobs"] if j["kind"] == "attention"]
    kept = {j["id"] for j in baseline["jobs"]}
    for job in baseline["jobs"]:
        job["depends_on"] = [dep for dep in job.get("depends_on", []) if dep in kept]
    reports = {}
    for name, trace in (("no-migration", baseline), ("with-migration", workload), ("slow-link", workload),
                        ("capacity-slower", workload), ("refresh-stress", workload), ("layer-isolation", isolation)):
        effective = copy.deepcopy(config)
        if name == "slow-link": effective["Frontend"]["base_die_link_GBps"] /= 32
        if name == "refresh-stress": effective["Frontend"]["base_die_link_GBps"] /= 256
        if name == "capacity-slower": effective["MemorySystem"]["Capacity"]["DRAM"]["timing"]["nCL"] = 26
        run = output / name
        effective, trace, process = run_case(run, effective, trace)
        process.check_returncode()
        reports[name] = validate_layered(run, effective, trace, process.stdout)
        if name == "layer-isolation": reports[name]["isolation"] = verify_isolation(run)
    check(reports["with-migration"]["per_layer"] == {"capacity": {"reads": 96, "writes": 96, "pim": 0},
          "compute": {"reads": 256, "writes": 256, "pim": 12}}, "layer routing totals")
    check(reports["slow-link"]["stop_cycle"] > reports["with-migration"]["stop_cycle"], "shared bandwidth has no feedback")
    check(reports["refresh-stress"]["refresh_commands"] > 0, "both refresh domains not exercised")
    summary = dict(schema_version=1, model="two independent HBM3-PIM timing domains; Capacity RD/WR subset, Compute PIM; one conservative shared Stack issue path",
                   limitations="one Channel and DMA, serial jobs/link stages, synthetic domain switch guard; PIM issue != arithmetic completion", scenarios=reports)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    manifest = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.rglob("*") if p.is_file()}
    (output / "source-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(output)


if __name__ == "__main__":
    main()
