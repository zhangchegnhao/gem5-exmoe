"""Integration checks for actual controller callbacks, gating and input rejection."""
from __future__ import annotations

import copy
import csv
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1] / "attacc_simulator/ramulator2"
sys.path.insert(0, str(ROOT / "scripts"))
from run_closed_loop import validate


def no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class ClosedLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tokenstack-controller-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = yaml.safe_load((ROOT / "config/tokenstack_closed_loop.yaml").read_text())
        self.workload = yaml.safe_load((ROOT / "traces/closed_loop_kv_v1.yaml").read_text())
        for job in self.workload["jobs"]:
            if job["kind"] != "attention":
                job["logical_bytes"] = 256
        self.config["Frontend"]["base_die_link_GBps"] = 8

    def run_backend(self, name: str, config: dict | None = None, workload: dict | None = None) -> tuple:
        config = copy.deepcopy(config or self.config)
        workload = copy.deepcopy(workload or self.workload)
        run = self.path / name
        run.mkdir()
        for key, filename in (("path", "workload.yaml"), ("request_log", "requests.csv"),
                              ("job_log", "jobs.csv"), ("resource_log", "resources.csv")):
            config["Frontend"][key] = str(run / filename)
        config["MemorySystem"]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(run / "commands.log")
        (run / "workload.yaml").write_text(yaml.safe_dump(workload))
        (run / "config.yaml").write_text(yaml.safe_dump(config))
        result = subprocess.run([str(REPO / "build/ramulator2"), "-f", str(run / "config.yaml")],
                                cwd=REPO, capture_output=True, text=True, timeout=10, preexec_fn=no_core)
        return run, config, workload, result

    def test_real_reads_writes_and_queued_descriptor_gate(self) -> None:
        run, config, workload, result = self.run_backend("valid")
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = validate(run, config, workload, result.stdout)
        self.assertEqual(stats["requests"], 34)  # 12 PIM + 11 RD + 11 WR
        self.assertEqual(stats["physical_read_bytes"], 352)
        self.assertEqual(stats["physical_write_bytes"], 352)
        self.assertEqual(stats["job_order"], ["promotion-0", "attention-0", "attention-1", "demotion-0"])
        # At 8 GB/s the target hop exceeds write recovery. A missing gate would
        # let early queued WRs return in the wrong phase and fail this run.
        self.assertGreater(stats["explicit_link_block_cycles"], 100)

    def test_reject_invalid_architecture_and_workloads(self) -> None:
        cases = []
        for key, value, message in (("buffer_bytes", 128, "exceeds buffer"),
                                    ("base_die_link_GBps", 0, "invalid link bandwidth"),
                                    ("compute_base", 0, "regions overlap"),
                                    ("compute_base", 1 << 40, "mapped DRAM span"),
                                    ("burst_bytes", 64, "transfer size")):
            config = copy.deepcopy(self.config)
            config["Frontend"][key] = value
            workload = copy.deepcopy(self.workload)
            if key == "burst_bytes":
                # Keep compressed bytes aligned so setup reaches the backend
                # transfer-size check rather than the earlier trace check.
                for job in workload["jobs"]:
                    if job["kind"] != "attention":
                        job["logical_bytes"] = 512
            cases.append((key + str(value), config, workload, message))
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["DRAM"]["org"]["channel"] = 2
        cases.append(("channels", config, self.workload, "single-channel"))
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["Controller"]["tokenstack_closed_loop"] = False
        cases.append(("disabled", config, self.workload, "enabled closed-loop"))
        for name, change, message in (
            ("unaligned", {"logical_bytes": 257}, "fractional"),
            ("bounds", {"offset": 1048576}, "bounds failure"),
            ("unknown", {"depends_on": ["missing"]}, "unknown dependency"),
            ("cycle", {"depends_on": ["demotion-0"]}, "dependency cycle"),
        ):
            workload = copy.deepcopy(self.workload)
            workload["jobs"][0].update(change)
            cases.append((name, self.config, workload, message))
        for name, config, workload, message in cases:
            with self.subTest(name=name):
                _, _, _, result = self.run_backend(name, config, workload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_audit_rejects_missing_duplicate_bytes_and_time(self) -> None:
        run, config, workload, result = self.run_backend("audit")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = run / "requests.csv"
        with path.open() as stream:
            reader = csv.DictReader(stream)
            fields, original = reader.fieldnames, list(reader)
        corrupted = [original[1:], original + [original[0]]]
        for field, value in (("bytes", "1"), ("callback_cycle", "0"), ("command_cycle", "0")):
            rows = copy.deepcopy(original)
            next(row for row in rows if row["phase"] == "read")[field] = value
            corrupted.append(rows)
        for rows in corrupted:
            with path.open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaises(AssertionError):
                validate(run, config, workload, result.stdout)


if __name__ == "__main__":
    unittest.main()
