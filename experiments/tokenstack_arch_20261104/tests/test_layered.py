"""Actual two-domain isolation, shared issue gating and routing integration tests."""
from __future__ import annotations

import copy
import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_layered import REPO, no_core, run_case, validate_layered, verify_isolation


class LayeredTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="tokenstack-layer-test-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.config = yaml.safe_load((ROOT / "config/tokenstack_layered.yaml").read_text())
        self.workload = yaml.safe_load((ROOT / "traces/closed_loop_kv_v1.yaml").read_text())
        for job in self.workload["jobs"]:
            if job["kind"] != "attention": job["logical_bytes"] = 256

    def test_real_layer_state_isolation(self) -> None:
        workload = yaml.safe_load((ROOT / "traces/layer_isolation_v1.yaml").read_text())
        run = self.path / "isolation"
        config, trace, process = run_case(run, self.config, workload)
        self.assertEqual(process.returncode, 0, process.stderr)
        validate_layered(run, config, trace, process.stdout)
        self.assertTrue(verify_isolation(run)["compute_pim_broadcast_isolated"])

    def test_migration_routes_bursts_and_layer_specific_latency(self) -> None:
        for name, ncl in (("base", 19), ("capacity-slower", 26)):
            config = copy.deepcopy(self.config)
            config["MemorySystem"]["Capacity"]["DRAM"]["timing"]["nCL"] = ncl
            run = self.path / name
            config, trace, process = run_case(run, config, self.workload)
            self.assertEqual(process.returncode, 0, process.stderr)
            summary = validate_layered(run, config, trace, process.stdout)
            self.assertEqual(summary["requests"], 34)
            self.assertEqual(summary["per_layer"], {"capacity": {"reads": 3, "writes": 3, "pim": 0},
                                                    "compute": {"reads": 8, "writes": 8, "pim": 12}})

    def test_invalid_domain_setup_and_direct_router_inputs(self) -> None:
        invalid = []
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["Capacity"]["DRAM"]["timing"]["preset"] = "HBM3_4.8Gbps"
        invalid.append(("clock", config, "clock mismatch"))
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["Compute"]["DRAM"]["org"]["channel"] = 2
        invalid.append(("channels", config, "unsupported layer"))
        config = copy.deepcopy(self.config)
        config["Frontend"]["compute_base"] *= 2
        invalid.append(("map", config, "layer map mismatch"))
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["domain_switch_guard_cycles"] = 0
        invalid.append(("guard", config, "switch guard"))
        config = copy.deepcopy(self.config)
        config["MemorySystem"]["Capacity"]["Controller"]["tokenstack_closed_loop"] = False
        invalid.append(("disabled", config, "unsupported layer"))
        for name, config, message in invalid:
            with self.subTest(name=name):
                _, _, process = run_case(self.path / name, config, self.workload)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(message, process.stderr)
        for name, text, message in (("capacity-pim", "PIM_MAC_AB 0x0\n", "PIM is forbidden"),
                                    ("gap", "LD 0x200000\n", "outside layer regions"),
                                    ("unaligned", "LD 0x1\n", "unaligned/out-of-bounds")):
            with self.subTest(name=name):
                run = self.path / name
                # Generate domain-local command log paths, then use the generic
                # frontend to reach the memory API independently of its driver.
                config, _, _ = run_case(run, self.config, self.workload)
                path = run / "direct.trace"
                path.write_text(text)
                config["Frontend"] = {"impl": "PIMLoadStoreTrace", "clock_ratio": 1, "path": str(path)}
                (run / "direct.yaml").write_text(yaml.safe_dump(config))
                process = subprocess.run([str(REPO / "build/ramulator2"), "-f", str(run / "direct.yaml")],
                                         cwd=REPO, text=True, capture_output=True, timeout=10, preexec_fn=no_core)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(message, process.stderr)

    def test_audit_rejects_layer_alias_and_ungranted_commands(self) -> None:
        run = self.path / "audit"
        config, trace, process = run_case(run, self.config, self.workload)
        self.assertEqual(process.returncode, 0, process.stderr)
        path = run / "requests.csv"
        original = path.read_text()
        with path.open() as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        rows[0]["layer_id"] = str(1 - int(rows[0]["layer_id"]))
        with path.open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        with self.assertRaisesRegex(AssertionError, "layer routing"):
            validate_layered(run, config, trace, process.stdout)
        path.write_text(original)
        grants = run / "grants.csv"
        with grants.open() as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        target = next(row for row in rows if row["owner_layer"] == "0")
        target["owner_layer"] = "1"
        with grants.open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        with self.assertRaises(AssertionError):
            validate_layered(run, config, trace, process.stdout)


if __name__ == "__main__":
    unittest.main()
