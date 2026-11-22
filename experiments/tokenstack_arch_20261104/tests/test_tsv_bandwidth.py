from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from run_hardware import run_case,validate_fabric,audit_resource_calendar,records
from throughput import provision,read_stream,dram_ceiling_GBps


class TSVBandwidthTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/'config/tokenstack_hardware.yaml').read_text())
        self.tmp=tempfile.TemporaryDirectory(prefix='tsv-bandwidth-');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)

    def test_shared_capacity_and_nonadjacent_direction_guard(self):
        cfg=provision(self.config,32/0.769)
        def r(start,end,bytes_,direction):
            return dict(start_cycle=str(start),end_cycle=str(end),bytes=str(bytes_),direction=str(direction))
        rows=[r(0,2,32,1),r(0,2,32,1)]
        stats=audit_resource_calendar({'tsv/s0':rows},cfg)['tsv/s0']
        self.assertEqual(stats['busy_cycles'],2)
        with self.assertRaisesRegex(AssertionError,'bandwidth exceeded'):
            audit_resource_calendar({'tsv/s0':rows+[r(0,2,32,1)]},cfg)
        # A neutral coordinator interval between records must not hide the
        # still-live opposite direction from a merely adjacent-row check.
        with self.assertRaisesRegex(AssertionError,'turnaround'):
            audit_resource_calendar({'tsv/s0':[r(0,10,16,1),r(1,2,1,0),r(3,4,1,2)]},cfg)
        with self.assertRaisesRegex(AssertionError,'overlap'):
            audit_resource_calendar({'channel/s0/ch0/pc0/data':rows},cfg)

    def test_live_channel_scaling_and_TSV_bottleneck(self):
        reports={}
        for name,channels,bandwidth in [('single',1,896),('four',4,896),('sixteen',16,896),('limited',16,112)]:
            cfg=provision(self.config,bandwidth);graph=read_stream(cfg,channels,bursts=32)
            path=self.path/name;run_case(path,cfg,graph).check_returncode()
            summary=validate_fabric(path,cfg,graph)
            reports[name]=summary['resource_occupancy']['tsv/s0']['effective_GBps']
            self.assertLessEqual(reports[name],min(bandwidth,dram_ceiling_GBps(cfg,channels)))
        self.assertGreater(reports['four'],reports['single']*2.5)
        self.assertGreater(reports['sixteen'],300)  # old whole-stack ceiling was 20.81 GB/s
        self.assertGreater(reports['limited'],80)
        self.assertGreater(reports['sixteen'],reports['limited']*2)

    def test_multiple_dies_do_not_multiply_shared_bandwidth(self):
        cfg=provision(self.config,112);graph=read_stream(cfg,16,dies=4,bursts=8)
        path=self.path/'dies';run_case(path,cfg,graph).check_returncode()
        stats=validate_fabric(path,cfg,graph)['resource_occupancy']['tsv/s0']
        self.assertLessEqual(stats['effective_GBps'],112)
        self.assertGreater(stats['effective_GBps'],70)
        self.assertLessEqual(stats['utilization'],1)
        self.assertLessEqual(stats['bandwidth_utilization'],1)


if __name__=='__main__':unittest.main()
