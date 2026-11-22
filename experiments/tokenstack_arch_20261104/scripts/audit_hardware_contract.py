"""H0 static evidence/units audit. This does not run a hardware simulation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
RAMULATOR = WORKSPACE / "attacc_simulator/ramulator2"
CLASSES = {"paper_arch", "paper_eval", "attacc_inherited", "engineering_assumption", "derived"}


class AuditError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capacity_bytes(geometry: list[int], dq: int, prefetch: int) -> int:
    require(len(geometry) == 7, "geometry must have CH/PC/rank/BG/bank/row/column")
    require(all(type(x) is int and x > 0 and x & (x - 1) == 0 for x in geometry),
            "bit-slice geometry requires positive power-of-two axes")
    require(dq > 0 and prefetch > 0 and dq * prefetch % 8 == 0, "invalid burst width")
    return math.prod(geometry) * dq * prefetch // 8


def unit_bytes(record: dict[str, Any]) -> int:
    scales = {"B": 1, "GB": 10**9, "GiB": 2**30}
    require(record["unit"] in scales, "unrecognized capacity unit")
    return record["value"] * scales[record["unit"]]


def validate_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    require(ledger["schema_version"] == 1, "unsupported schema")
    required = {"value", "unit", "scope", "class", "source", "status", "current", "validation"}
    for name, param in ledger["parameters"].items():
        require(required <= param.keys(), f"missing provenance fields: {name}")
        require(param["class"] in CLASSES, f"invalid source class: {name}")
        require(bool(param["source"]), f"missing sources: {name}")
        for ref in param["source"]:
            require(ref in ledger["sources"], f"unknown source: {ref}")
        if param["value"] is None:
            require(param["status"] == "unresolved" and "question" in param,
                    f"unknown value requires unresolved question: {name}")
        if param["class"] == "derived":
            require(bool(param.get("formula")), f"derived value lacks formula: {name}")
        if "question" in param:
            require(param["question"] in ledger["open_questions"], f"unknown question: {name}")
    blockers = [key for key, q in ledger["open_questions"].items()
                if q["status"] == "open" and q["blocks_paper_freeze"]]
    require(type(ledger["paper_spec_frozen"]) is bool, "freeze flag must be boolean")
    require(not (blockers and ledger["paper_spec_frozen"]), "open critical questions forbid paper freeze")
    params = ledger["parameters"]
    for key, scope in (("tsv_bandwidth", "stack_aggregate_dma"), ("ucie_bandwidth", "stack_host_interface")):
        require(params[key]["scope"] == scope and params[key]["unit"] == "GB/s"
                and params[key]["class"] == "paper_eval", "link bandwidth must retain per-stack scope and decimal unit")
    profile = ledger["profiles"]["fig3_binary_geometry_assumption_v1"]
    sizes = {}
    for kind in ("capacity", "compute"):
        size = capacity_bytes(profile[f"{kind}_geometry"], profile["dq_bits"], profile["prefetch"])
        require(size == profile[f"{kind}_die_bytes"] == unit_bytes(params[f"{kind}_per_die"]),
                f"{kind} geometry/capacity mismatch (possible double halving or wrong unit)")
        geometry = profile[f"{kind}_geometry"]
        require(geometry[2] * geometry[3] * geometry[4] == params[f"{kind}_banks_per_pc"]["value"],
                f"{kind} Bank/PC mismatch")
        require(profile[f"{kind}_dies"] == params[f"{kind}_dies"]["value"], "die count mismatch")
        sizes[kind] = size
    if profile["runtime_supported"]:
        refresh=profile["refresh_timing"]
        require(isinstance(refresh,dict) and all(refresh.get(key,0)>0 for key in ("nRFC","nREFISB","nREFI"))
                and refresh.get("class")=="engineering_assumption" and bool(refresh.get("source")),
                "runtime/refresh support requires explicit sourced assumptions")
    stacks = params["stacks_per_card"]["value"]
    units = {}
    for kind, size in sizes.items():
        figure_card = size * profile[f"{kind}_dies"] * stacks
        reported = unit_bytes(params[f"card_{kind}_reported"])
        units[kind] = {"figure_derived_card_bytes": figure_card, "table2_literal_card_bytes": reported,
                       "difference_bytes": figure_card - reported,
                       "relative_difference": figure_card / reported - 1}
        require(figure_card != reported, "unexpectedly concealed GB/GiB ambiguity")
    return {"blockers": blockers, "candidate_die_bytes": sizes, "unit_alternatives": units}


def parse_org(source: str) -> tuple[int, int, list[int]]:
    match = re.search(r'\{"HBM3_8Gb_2R",\s*\{8<<10,\s*(\d+),\s*\{([^}]+)\}', source)
    require(match is not None, "cannot locate audited organization")
    # Strict grammar, never eval() C++ source.
    values = []
    for token in match[2].split(","):
        parts = token.strip().split("<<")
        require(len(parts) <= 2 and all(x.strip().isdigit() for x in parts), "unexpected org expression")
        values.append(int(parts[0]) << int(parts[1]) if len(parts) == 2 else int(parts[0]))
    prefetch = re.search(r'm_internal_prefetch_size\s*=\s*(\d+)', source)
    require(prefetch is not None, "missing prefetch")
    return int(match[1]), int(prefetch[1]), values


def parse_timing(source: str) -> dict[str, int]:
    header = re.search(r'//\s+name\s+(rate\s+nBL[^\n]+)', source)
    row = re.search(r'\{"HBM3_5.2Gbps",\s*\{([^}]+)\}', source)
    require(header is not None and row is not None, "cannot locate timing table")
    names, values = header[1].split(), [int(x.strip()) for x in row[1].split(",")]
    require(len(names) == len(values), "timing columns drifted")
    return dict(zip(names, values))


def audit(root: Path = ROOT) -> dict[str, Any]:
    ledger = yaml.safe_load((root / "spec/hardware_parameters.yaml").read_text())
    report = validate_ledger(ledger)
    params = ledger["parameters"]
    reference = json.loads((root / "references/tokenstack_source_20261105.json").read_text())
    pdf = root / "references" / reference["local_pdf"]
    require(reference["arxiv_id"] == ledger["paper_revision"], "paper revision drift")
    require(sha256(pdf) == reference["sha256"], "paper hash mismatch")
    paths = {key: (root / source["path"]).resolve() for key, source in ledger["sources"].items()}
    for key, path in paths.items():
        require(path.is_file(), f"missing evidence source: {key}: {path}")
    dram, mapper = paths["dram"].read_text(), paths["mapper"].read_text()
    controller, scheduler = paths["controller"].read_text(), paths["scheduler"].read_text()
    dq, prefetch, org = parse_org(dram)
    require(org == params["attacc_geometry"]["value"], "organization source drift")
    require(dq == params["attacc_dq"]["value"] and prefetch == params["internal_prefetch"]["value"],
            "DQ/prefetch source drift")
    require('//m_addr_bits[m_num_levels - 1] -= calc_log2(m_dram->m_internal_prefetch_size);' in mapper,
            "column prefetch interpretation changed; re-audit mapper")
    attacc = yaml.safe_load(paths["attacc_config"].read_text())
    require(attacc["MemorySystem"]["DRAM"]["org"]["channel"] == params["attacc_channels_per_stack"]["value"],
            "AttAcc channel source drift")
    config = yaml.safe_load(paths["prototype"].read_text())
    for field, param in (("burst_bytes", "burst_bytes"), ("buffer_bytes", "buffer_bytes"),
                         ("queue_depth", "queue_depth"), ("region_bytes", "mapped_window"),
                         ("base_die_link_GBps", "tsv_bandwidth")):
        require(config["Frontend"][field] == params[param]["current"], f"prototype {field} drift")
    require(config["MemorySystem"]["domain_switch_guard_cycles"] == params["switch_guard"]["current"],
            "switch guard drift")
    for kind in ("Capacity", "Compute"):
        domain = config["MemorySystem"][kind]
        require(domain["DRAM"]["org"] == {"preset": "HBM3_8Gb_2R", "channel": org[0]},
                f"{kind} organization changed; re-audit")
        require(domain["DRAM"]["timing"] == {"preset": params["dram_timing"]["value"]},
                f"{kind} timing overrides require re-audit")
        require(domain["Controller"]["tokenstack_closed_loop"] is True, "closed-loop write callback disabled")
    timing = parse_timing(dram)
    require('1E6 / (m_timing_vals("rate") / 4)' in dram, "tCK formula changed")
    tck = int(1e6 / (timing["rate"] / 4))
    require(tck == params["tck"]["value"], "tCK drift")
    rd, wr = timing["nCL"] + timing["nBL"], timing["nCWL"] + timing["nBL"] + timing["nWR"]
    require((rd, wr) == (21, 29), "retirement timing drift")
    require('m_read_latency = m_timing_vals("nCL") + m_timing_vals("nBL")' in dram,
            "read completion formula changed")
    require(re.search(r'req\.depart = m_clk \+ m_dram->m_timing_vals\("nCWL"\)\s*'
                      r'\+ m_dram->m_timing_vals\("nBL"\)\s*\+ m_dram->m_timing_vals\("nWR"\)', controller) is not None,
            "write completion formula changed")
    require(re.search(r'else if \(req_it->callback\)\s*\{\s*req_it->depart = m_clk;', controller) is not None,
            "PIM issue callback changed; inspect result semantics")
    require('candidate->depart = cast_parent<IDRAMController>()->get_clk();' in scheduler,
            "barrier event changed")
    require((timing["nCCDAB"], timing["nCCDSB"], timing["nCCDS"], timing["nCCDL"]) == (6, 6, 2, 4),
            "PIM command spacing drift")
    ratio = params["compressed_ratio"]["value"]
    require(ratio == [config["Frontend"]["compressed_num"], config["Frontend"]["compressed_den"]],
            "compression config drift")
    require(ratio[0] * 32 == ratio[1] * (params["cold_k_precision"]["value"] + params["cold_v_precision"]["value"]),
            "K8V4 payload ratio mismatch")
    refresh_cases = [int(x) for x in re.findall(r'case (\d+):\s+return \d+;', dram)]
    candidate = ledger["profiles"]["fig3_binary_geometry_assumption_v1"]
    densities = {kind: report["candidate_die_bytes"][kind] * 8 // candidate[f"{kind}_geometry"][0] // 2**20
                 for kind in ("capacity", "compute")}
    require(all(x not in refresh_cases for x in densities.values()), "refresh support changed; re-audit candidate")
    require('unsupported density requires explicit nRFC and nREFISB' in dram and 'if (density_id < 0' in dram,
            "explicit refresh safety guard missing")
    report.update({
        "schema_version": 1, "audit_status": "passed", "audit_kind": "static_evidence_and_dimensions",
        "hardware_simulation_run": False, "paper_spec_frozen": ledger["paper_spec_frozen"],
        "milestone_status": "H0_partial_paper_freeze_blocked", "candidate_runtime_supported": candidate["runtime_supported"],
        "parameter_count": len(params), "paper_sha256": reference["sha256"],
        "checks": ["H0-provenance", "H0-paper-pin", "H0-geometry", "H0-capacity", "H0-units",
                   "H0-no-double-halving", "H0-config", "H0-timing", "H0-completion", "H0-k8v4",
                   "H0-link-budget-scope", "H0-refresh-unsupported"],
        "prototype": {"domain_decodable_bytes": capacity_bytes(org, dq, prefetch),
                      "mapped_window_bytes": config["Frontend"]["region_bytes"],
                      "banks_per_pc_across_ranks": math.prod(org[2:5]),
                      "tck_ps": tck, "preset_trailing_tck_ps_overridden": timing["tCK_ps"],
                      "read_issue_to_data_cycles": rd, "write_issue_to_recovery_cycles": wr,
                      "pim_result_ready_cycles": None, "pim_bytes": None},
        "candidate_density_Mib_per_channel": densities,
        "link_budget": {"tsv_bytes_per_second_per_stack": params["tsv_bandwidth"]["value"] * 10**9,
                        "ucie_bytes_per_second_per_stack": params["ucie_bandwidth"]["value"] * 10**9,
                        "source_and_target_hops_debit_same_budget": True,
                        "physical_overlap_with_nBL_verified": False},
        "sources": {key: {"path": str(path), "sha256": sha256(path)} for key, path in paths.items()},
        "limitations": ["Paper unit/Bank ambiguity unresolved", "Runtime requires explicit engineering refresh timings",
                        "Legacy PIM callback is issue; Fabric adds configured result tail", "No physical TSV overlap calibration"],
    })
    return report


def command_output(args: list[str], cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return {"argv": args, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="new result directory name; existing results are never overwritten")
    parser.add_argument("--require-paper-freeze", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.run_id) is not None, "invalid run ID")
    output = ROOT / "results" / args.run_id
    output.mkdir(exist_ok=False)
    (output / "command.json").write_text(json.dumps({"argv": sys.argv, "cwd": str(Path.cwd()),
                                                     "time_utc": datetime.now(timezone.utc).isoformat(),
                                                     "python": sys.version}, indent=2) + "\n")
    try:
        report = audit()
        gate_passed = not args.require_paper_freeze or report["paper_spec_frozen"]
        report["paper_freeze_gate"] = {"required": args.require_paper_freeze, "passed": gate_passed}
        for directory in ("spec",):
            shutil.copytree(ROOT / directory, output / directory)
        (output / "scripts").mkdir()
        for relative in ("scripts/audit_hardware_contract.py", "tests/test_hardware_contract.py",
                         "config/tokenstack_layered.yaml", "references/tokenstack_source_20261105.json"):
            target = output / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        # Retain the exact inspected source text as well as its hash, not only HEAD.
        for key, entry in report["sources"].items():
            path = Path(entry["path"])
            if path.suffix != ".pdf":
                target = output / "source_snapshot" / key / path.name
                target.parent.mkdir(parents=True)
                shutil.copy2(path, target)
        repos = {name: {"head": command_output(["git", "rev-parse", "HEAD"], path),
                        "status": command_output(["git", "status", "--short"], path)}
                 for name, path in (("attacc", RAMULATOR.parent), ("ramulator", RAMULATOR),
                                    ("gem5", WORKSPACE / "gem5-src"))}
        for relative in ("build/ramulator2", "libramulator.so", "build/CMakeCache.txt"):
            path = RAMULATOR / relative
            repos[relative] = {"exists": path.is_file(), "sha256": sha256(path) if path.is_file() else None}
        (output / "environment.json").write_text(json.dumps(repos, indent=2) + "\n")
        (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
        message = (f"Static audit passed: {report['parameter_count']} parameters; "
                   f"paper frozen={report['paper_spec_frozen']}; blockers={','.join(report['blockers'])}; "
                   f"runtime supported={report['candidate_runtime_supported']}\n")
        if not gate_passed:
            message += "Paper freeze gate rejected: critical questions remain open.\n"
        (output / "audit.log").write_text(message)
        print(message, end="")
        return 0 if gate_passed else 2
    except (AuditError, KeyError, OSError) as exc:
        (output / "audit.json").write_text(json.dumps({"audit_status": "failed", "error": str(exc)}, indent=2) + "\n")
        raise


if __name__ == "__main__":
    sys.exit(main())
