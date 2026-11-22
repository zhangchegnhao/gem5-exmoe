from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from gpu_system import load_system,configure_system,CapacityAllocator,gpu_kernel,compile_workload
from run_hardware import run_case,validate_fabric,records
from run_gpu_system import run_system,validate_system


class GPUSystemTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.config,self.contract=load_system()
        self.trace=yaml.safe_load((ROOT/'traces/a100a_decode_micro.yaml').read_text())
        self.tmp=tempfile.TemporaryDirectory(prefix='gpu-system-');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)

    def test_capacity_bandwidth_and_disjoint_allocation(self):
        self.assertEqual(self.contract['capacity_bytes']['total'],60*2**30)
        self.assertEqual(self.contract['capacity_bytes']['capacity_layers'],40*2**30)
        self.assertEqual(self.contract['capacity_bytes']['compute_layers'],20*2**30)
        self.assertEqual(self.contract['attacc_overrides']['OFF_MEM_BW_PER_DEVICE'],2560e9)
        self.assertEqual(self.contract['gpu']['peak_flops_per_second'],312e12)
        self.assertEqual(self.contract['SMs'],108)
        arena=CapacityAllocator(self.config,256)
        self.assertEqual({p['stack'] for p in arena.allocate('stripes',5*256)},set(range(5)))
        arena.cursor=arena.limit//256-1
        last=arena.allocate('last',256)[0]
        die=arena.topo.find(last['address'],last['bytes'])
        self.assertEqual(last['address']+last['bytes'],die.base+die.size)
        with self.assertRaisesRegex(ValueError,'capacity overflow'):arena.allocate('overflow',1)

    def test_configuration_rejects_unsupported_and_invalid_inputs(self):
        for key,value in [('devices',2),('model','H100'),('compute_utilization',0),('compute_utilization',float('nan')),
                          ('compute_utilization',1.01),('launch_overhead_cycles',-1),('precision','W8A8')]:
            with self.subTest(key=key,value=value):
                profile=copy.deepcopy(self.profile);profile['gpu'][key]=value
                with self.assertRaises(ValueError):configure_system(profile,self.config)
        profile=copy.deepcopy(self.profile);profile['stacks']=1
        with self.assertRaises(ValueError):configure_system(profile,self.config)

    def test_one_shared_gpu_lane_and_compute_rounding(self):
        nodes=[];cycles=0
        for name in ('kernel_a','kernel_b'):
            node,meta=gpu_kernel(self.config,name,512,512,512,depends_on=[])
            nodes.append(node);cycles+=meta['total_cycles']
            self.assertLess(meta['compute_rounding_ps'],self.config['clock_ps'])
        graph={'schema_version':1,'tasks':nodes};path=self.path/'lanes'
        run_case(path,self.config,graph).check_returncode();summary=validate_fabric(path,self.config,graph)
        rows=sorted(records(path/'tasks.csv'),key=lambda r:int(r['start_cycle']))
        self.assertGreaterEqual(int(rows[1]['start_cycle']),int(rows[0]['complete_cycle']))
        self.assertEqual(summary['resource_occupancy']['gpu_compute/card0']['busy_cycles'],cycles)
        # Work cannot silently execute without the explicit GPU system profile.
        config=copy.deepcopy(self.config);del config['gpu_system']
        proc=run_case(self.path/'missing-profile',config,graph)
        self.assertNotEqual(proc.returncode,0)
        self.assertIn('GPU work requires explicit',proc.stdout+proc.stderr)

    def test_live_pipeline_timing_bytes_and_independent_background(self):
        # Non-burst-sized tensor proves logical payload and physical burst bytes
        # are not conflated. Delayed background completion cannot extend TPOT-like latency.
        trace=copy.deepcopy(self.trace);trace.update(hidden_dim=81,issue_cycle=31,background_issue_cycle=3000)
        baseline=run_system(self.path/'baseline',self.profile,self.config,trace)
        self.assertEqual(baseline['foreground_latency_ps'],(baseline['foreground_complete_cycle']-31)*769)
        self.assertLess(baseline['foreground_complete_cycle'],3000)
        self.assertGreater(baseline['background_drain_after_foreground_cycles'],0)
        gpu=baseline['by_source']['gpu'];self.assertGreater(gpu['write_bytes'],gpu['mask_valid_write_bytes'])
        for case in ('compute','memory'):
            profile,config=copy.deepcopy(self.profile),copy.deepcopy(self.config)
            if case=='compute':profile['gpu']['compute_utilization']=0.0008
            else:config['resources']['ucie_GBps']=1
            result=run_system(self.path/case,profile,config,trace)
            self.assertGreater(result['foreground_latency_cycles'],baseline['foreground_latency_cycles'])
            self.assertEqual(result['by_source'],baseline['by_source'])
        # Independent audit must catch an altered work completion, not merely
        # trust that requests drained from the same simulator.
        graph,manifest=compile_workload(self.profile,self.config,trace)
        import csv
        rows=records(self.path/'baseline/tasks.csv')
        for row in rows:
            if row['task_id']=='query_projection':row['complete_cycle']=str(int(row['complete_cycle'])+1)
        with (self.path/'baseline/tasks.csv').open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        with self.assertRaises(AssertionError):validate_system(self.path/'baseline',self.config,graph,manifest)


if __name__=='__main__':unittest.main()
