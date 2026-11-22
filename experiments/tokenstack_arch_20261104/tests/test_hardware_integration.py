"""Finite mixed-load liveness, migration conflicts, and independent audit rejection."""
from __future__ import annotations
import copy
import csv
from pathlib import Path
import random
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
from hardware_topology import Topology
from attention_hardware import attention_graph
from kv_pipeline import migration_graph
from host_paths import gpu_access
from run_hardware import run_case,validate_fabric,records


def mixed_graph(config: dict) -> dict:
    # Fixed placement strategy only; no adaptive algorithm/system throughput model.
    rng=random.Random(config["seed"]);topo=Topology(config);tasks=[]
    promotion,pm=migration_graph(config,tokens=12,prefix="promote",block=0,issue=8)
    attention,am=attention_graph(config,12,config["base_die"]["head_dim"],prefix="foreground",depends_on=[pm["done_task"]])
    demotion,dm=migration_graph(config,tokens=12,prefix="background",block=1,kind="demotion")
    tasks+=demotion["tasks"]+promotion["tasks"]+attention["tasks"]
    for i in range(24):
        die=topo.dies[rng.randrange(len(topo.dies))]
        # Ordinary GPU data occupies the upper half of each die, away from KV.
        address=die.base+die.size//2+32*rng.randrange(32)
        tasks+=gpu_access(config,address,prefix=f"gpu{i}",write=i%3==0,issue=rng.randrange(60))["tasks"]
    return {"schema_version":1,"tasks":tasks}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text())
        self.temp=tempfile.TemporaryDirectory(prefix="tokenstack-integration-");self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)
    def simulate(self,name,config,graph):
        path=self.path/name;proc=run_case(path,config,graph);self.assertEqual(proc.returncode,0,proc.stderr)
        return path,validate_fabric(path,config,graph)
    def test_mixed_foreground_background_GPU_sources_finite_pressure(self):
        config=copy.deepcopy(self.config);config["resources"]["queue_depth_per_source"]=1
        config["dram"].update(nREFI=300,nRFC=32)
        graph=mixed_graph(config);path,summary=self.simulate("mixed",config,graph)
        self.assertGreater(summary["refresh_commands"],0)
        self.assertEqual({r["source"] for r in records(path/"tasks.csv")},{"gpu","pim","promotion","demotion"})
        self.assertLess(summary["complete_cycle"],20000)
    def test_conflicting_same_page_migrations_serialize_and_release(self):
        one,_=migration_graph(self.config,tokens=4,prefix="first",block=0)
        two,_=migration_graph(self.config,tokens=4,prefix="second",block=0,kind="demotion")
        graph={"schema_version":1,"tasks":one["tasks"]+two["tasks"]}
        path,_=self.simulate("conflict",self.config,graph)
        rows=records(path/"buffers.csv");intervals={}
        for row in rows:intervals.setdefault(row["page_id"],{})[row["event"]]=int(row["cycle"])
        a,b=intervals.values()
        self.assertTrue(a["release"]<=b["allocate"] or b["release"]<=a["allocate"])
    def test_audit_rejects_resource_overlap_mask_corruption_and_buffer_leak(self):
        graph,_=migration_graph(self.config,tokens=4)
        path,_=self.simulate("tamper",self.config,graph)
        for filename,mutate in [
            ("resources.csv",lambda rows:rows.append(dict(rows[0]))),
            ("requests.csv",lambda rows:rows[0].update(byte_mask="1")),
            ("buffers.csv",lambda rows:rows.pop())]:
            file=path/filename;original=file.read_text()
            with file.open() as stream:reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
            mutate(rows)
            with file.open('w') as stream:writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
            with self.assertRaises(AssertionError):validate_fabric(path,self.config,graph)
            file.write_text(original)
    def test_two_stack_buffer_pools_are_independent(self):
        config=copy.deepcopy(self.config);config["stacks"]=2;tasks=[]
        for stack in range(2):
            graph,_=migration_graph(config,tokens=12,stack=stack,prefix=f"s{stack}")
            tasks+=graph["tasks"]
        _,summary=self.simulate("two",config,{"schema_version":1,"tasks":tasks})
        self.assertEqual(summary["buffer_peak_bytes_by_stack_head_group"],{0:512,1:512})

if __name__=="__main__":unittest.main()
