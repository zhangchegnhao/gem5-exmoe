"""Reject incorrect capacity, units, provenance and premature paper freeze."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_hardware_contract import AuditError, audit, capacity_bytes, validate_ledger


class HardwareContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = yaml.safe_load((ROOT / "spec/hardware_parameters.yaml").read_text())

    def test_pinned_source_config_and_dimension_audit(self) -> None:
        report = audit()
        self.assertEqual(report["prototype"]["domain_decodable_bytes"], 2**30)
        self.assertEqual(report["candidate_die_bytes"], {"capacity": 2**31, "compute": 2**30})
        self.assertEqual(report["candidate_density_Mib_per_channel"], {"capacity": 1024, "compute": 512})
        self.assertFalse(report["paper_spec_frozen"])
        self.assertTrue(report["candidate_runtime_supported"])
        self.assertIsNone(report["prototype"]["pim_result_ready_cycles"])

    def test_double_halving_rejected(self) -> None:
        self.ledger["profiles"]["fig3_binary_geometry_assumption_v1"]["compute_geometry"][5] //= 2
        with self.assertRaisesRegex(AuditError, "possible double halving"):
            validate_ledger(self.ledger)

    def test_silent_unit_relabel_rejected(self) -> None:
        self.ledger["parameters"]["capacity_per_die"]["unit"] = "GB"
        with self.assertRaisesRegex(AuditError, "wrong unit"):
            validate_ledger(self.ledger)

    def test_open_questions_forbid_freeze(self) -> None:
        self.ledger["paper_spec_frozen"] = True
        with self.assertRaisesRegex(AuditError, "forbid paper freeze"):
            validate_ledger(self.ledger)

    def test_unknown_requires_question_and_cannot_be_verified(self) -> None:
        self.ledger["parameters"]["pim_tail_latency"]["status"] = "verified_source"
        with self.assertRaisesRegex(AuditError, "unknown value"):
            validate_ledger(self.ledger)

    def test_missing_provenance_rejected(self) -> None:
        del self.ledger["parameters"]["switch_guard"]["source"]
        with self.assertRaisesRegex(AuditError, "missing provenance"):
            validate_ledger(self.ledger)

    def test_non_power_of_two_geometry_rejected(self) -> None:
        geometry = copy.copy(self.ledger["profiles"]["fig3_binary_geometry_assumption_v1"]["capacity_geometry"])
        geometry[5] = 4000
        with self.assertRaisesRegex(AuditError, "power-of-two"):
            capacity_bytes(geometry, 32, 8)

    def test_unsupported_refresh_cannot_claim_executable(self) -> None:
        self.ledger["profiles"]["fig3_binary_geometry_assumption_v1"]["runtime_supported"] = True
        self.ledger["profiles"]["fig3_binary_geometry_assumption_v1"]["refresh_timing"] = None
        with self.assertRaisesRegex(AuditError, "runtime/refresh support"):
            validate_ledger(self.ledger)

    def test_stack_bandwidth_cannot_be_granted_per_layer(self) -> None:
        self.ledger["parameters"]["tsv_bandwidth"]["scope"] = "each_layer"
        with self.assertRaisesRegex(AuditError, "per-stack scope"):
            validate_ledger(self.ledger)


if __name__ == "__main__":
    unittest.main()
