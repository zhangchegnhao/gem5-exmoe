from pathlib import Path
import sys
import unittest
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenstack_model import (
    LayerKind,
    MigrationSpec,
    ResourceArbiter,
    load_attacc_pim_trace,
    load_topology,
)
from ramulator_bridge import load_retirements, replay_requests


CONFIG = Path(__file__).resolve().parents[1] / "config" / "tokenstack_minimal.yaml"


class TokenStackModelTests(unittest.TestCase):
    def test_topology_has_capacity_and_compute_layers(self):
        stack = load_topology(str(CONFIG))
        self.assertEqual(len(stack.layers), 4)
        self.assertEqual(sum(layer.kind is LayerKind.CAPACITY for layer in stack.layers), 3)
        self.assertEqual(sum(layer.kind is LayerKind.COMPUTE for layer in stack.layers), 1)

    def test_address_mapping_keeps_domains_separate(self):
        stack = load_topology(str(CONFIG))
        capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
        compute = stack.map_address(0x1000, LayerKind.COMPUTE)
        self.assertIs(capacity.layer_kind, LayerKind.CAPACITY)
        self.assertIs(compute.layer_kind, LayerKind.COMPUTE)
        self.assertNotEqual(capacity.layer_id, compute.layer_id)

    def test_dma_serializes_and_demotes_with_compression(self):
        stack = load_topology(str(CONFIG))
        capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
        compute = stack.map_address(0x1000, LayerKind.COMPUTE)
        promotion = stack.migrate("p", capacity, compute, 8192, issue_cycle=0)
        demotion = stack.migrate("d", compute, capacity, 8192, issue_cycle=1, priority=1)
        self.assertGreaterEqual(demotion.start_cycle, promotion.complete_cycle)
        self.assertGreater(demotion.queue_cycles, 0)
        self.assertLess(demotion.transferred_bytes, demotion.source_bytes)
        self.assertEqual(stack.stats.promotions, 1)
        self.assertEqual(stack.stats.demotions, 1)

    def test_invalid_cross_stack_migration_is_rejected(self):
        stack = load_topology(str(CONFIG))
        capacity = stack.map_address(0x1000, LayerKind.CAPACITY, stack_id=0)
        compute = stack.map_address(0x1000, LayerKind.COMPUTE, stack_id=0)
        remote = compute.__class__(1, compute.layer_id, compute.layer_kind,
                                   compute.channel, compute.pseudo_channel,
                                   compute.bank_group, compute.bank, compute.row,
                                   compute.column)
        with self.assertRaises(ValueError):
            stack.migrate("bad", capacity, remote, 4096)

    def test_shared_arbiter_conserves_bytes_and_exposes_contention(self):
        stack = load_topology(str(CONFIG))
        trace = Path(__file__).resolve().parents[1] / "traces" / "attacc_pim_small.trace"
        pim_requests = load_attacc_pim_trace(trace, stack)
        capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
        compute = stack.map_address(0x1000, LayerKind.COMPUTE)
        migrations = [
            MigrationSpec("promotion", capacity, compute, 8192, issue_cycle=0, priority=0),
            MigrationSpec("demotion", compute, capacity, 8192, issue_cycle=10, priority=1),
        ]
        arbiter = ResourceArbiter(stack)
        results = arbiter.run(pim_requests, migrations)
        conservation = arbiter.conservation(results)
        self.assertTrue(conservation["logical_migration_bytes_conserved"])
        self.assertTrue(conservation["completion_cycle_matches_max_event"])
        self.assertEqual(conservation["pim_request_count"], 6)
        self.assertEqual(conservation["migration_physical_write_bytes"], 11264)
        self.assertGreater(sum(row.queue_cycles for row in results), 0)

    def test_k8v4_accounts_for_compressed_source_on_promotion(self):
        stack = load_topology(str(CONFIG))
        self.assertEqual(stack.migration_bytes(8192, LayerKind.CAPACITY), (3072, 8192))
        self.assertEqual(stack.migration_bytes(8192, LayerKind.COMPUTE), (8192, 3072))
        capacity = stack.map_address(0, LayerKind.CAPACITY)
        compute = stack.map_address(0, LayerKind.COMPUTE)
        result = ResourceArbiter(stack).run([], [
            MigrationSpec("promote", capacity, compute, 8192, 0, 0)])[0]
        self.assertEqual((result.physical_read_bytes, result.physical_write_bytes), (3072, 8192))
        self.assertEqual(result.tsv_cycles, 14)  # ceil(3072/896) + ceil(8192/896)

    def test_retirement_replay_does_not_resum_queue_latency(self):
        rows = [
            dict(source_id=0, type_id=7, address=0, channel=0, arrive_cycle=0,
                 retire_cycle=1, tck_ps=769),
            dict(source_id=1, type_id=4, address=0, channel=0, arrive_cycle=0,
                 retire_cycle=20, tck_ps=769),
            dict(source_id=2, type_id=13, address=0, channel=0, arrive_cycle=0,
                 retire_cycle=20, tck_ps=769),
        ]
        stack = load_topology(str(CONFIG))
        requests = replay_requests(rows, stack)
        self.assertEqual([r.duration_cycles for r in requests], [1, 19, 0])
        events = ResourceArbiter(stack).run(requests, [])
        self.assertEqual([r.complete_cycle for r in events], [1, 20, 20])
        self.assertEqual(sum(r.physical_read_bytes for r in events), 0)

    def test_retirement_parser_checks_ids_and_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = Path(folder) / "input.trace"
            trace.write_text("PIM_WR_GB 0x0\nPIM_BARRIER 0x0\n")
            output = Path(folder) / "retirement.csv"
            header = "schema_version,source_id,type_id,address,channel,arrive_cycle,retire_cycle,tck_ps,event\n"
            valid = "1,0,7,0,0,0,1,769,final_command_issued\n1,1,13,0,0,0,1,769,barrier_removed\n"
            output.write_text(header + valid)
            self.assertEqual(len(load_retirements(output, trace)), 2)
            output.write_text(header + valid.replace("1,1,13", "1,0,13"))
            with self.assertRaisesRegex(ValueError, "source_id"):
                load_retirements(output, trace)


if __name__ == "__main__":
    unittest.main()
