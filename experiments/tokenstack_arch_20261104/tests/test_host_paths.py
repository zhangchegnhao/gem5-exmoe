from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
from hardware_topology import Topology
from host_paths import cross_stack_copy,gpu_access
from run_hardware import run_case,validate_fabric,records,topology_graph

class HostPathTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text());self.config["stacks"]=2
        self.temp=tempfile.TemporaryDirectory(prefix="tokenstack-host-");self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)
    def simulate(self,name,config,graph):
        path=self.path/name;proc=run_case(path,config,graph)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return path,validate_fabric(path,config,graph)
    def test_cross_stack_exact_paths_and_bytes(self):
        topo=Topology(self.config);source=topo.dies[0].base;target=topo.dies[8].base
        graph,manifest=cross_stack_copy(self.config,source,target,128)
        path,summary=self.simulate("remote",self.config,graph)
        rows=records(path/"resources.csv")
        for resource,bytes_ in manifest["expected_resource_bytes"].items():
            self.assertEqual(sum(int(r["bytes"]) for r in rows if r["resource"]==resource),bytes_)
        self.assertEqual(summary["read_bytes"],summary["write_bytes"])
    def test_gpu_write_read_ports_and_host_bottleneck(self):
        topo=Topology(self.config);src=topo.dies[0].base;dst=topo.dies[8].base
        graph=gpu_access(self.config,src,write=True,bytes_=128,prefix="write")
        graph["tasks"]+=gpu_access(self.config,src,bytes_=128,prefix="read",depends_on=["write_egress"])["tasks"]
        path,_=self.simulate("gpu",self.config,graph)
        self.assertEqual(sum(int(r["bytes"]) for r in records(path/"resources.csv") if r["resource"]=="ucie/s0"),256)
        graph,_=cross_stack_copy(self.config,src,dst,256)
        _,fast=self.simulate("fast",self.config,graph)
        slow=copy.deepcopy(self.config);slow["resources"]["host_crossbar_GBps"]=1
        _,limited=self.simulate("slow",slow,graph)
        self.assertGreater(limited["complete_cycle"],fast["complete_cycle"])
    def test_five_stack_capacity_and_all_physical_trees(self):
        self.config["stacks"]=5;topo=Topology(self.config)
        path,summary=self.simulate("five",self.config,topology_graph(self.config))
        self.assertEqual(topo.total_bytes,60*2**30)
        self.assertEqual(summary["requests"],180)
        self.assertEqual({r["stack"] for r in records(path/"events.csv")},{"0","1","2","3","4"})
    def test_cross_card_and_invalid_destination_rejected(self):
        with self.assertRaisesRegex(ValueError,"NVLink external boundary"):
            cross_stack_copy(self.config,0,12*2**30,32,target_card=1)
        with self.assertRaises(ValueError):cross_stack_copy(self.config,0,0,32)
        with self.assertRaises(ValueError):cross_stack_copy(self.config,0,24*2**30,32)

if __name__=="__main__":unittest.main()
