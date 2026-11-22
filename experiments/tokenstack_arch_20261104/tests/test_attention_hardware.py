from __future__ import annotations
import copy
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
from hardware_topology import Topology
from attention_hardware import attention_graph,bank_local_attention
from run_hardware import run_case,validate_fabric,records

class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text())
        self.temp=tempfile.TemporaryDirectory(prefix="tokenstack-attention-");self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
    def simulate(self,name,config,graph):
        path=self.path/name;proc=run_case(path,config,graph)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return path,validate_fabric(path,config,graph)
    def test_layout_reference_against_dense_attention(self):
        rng=random.Random(260505639);topo=Topology(self.config)
        for tokens,dim in [(3,7),(17,19),(35,9)]:
            q=[rng.uniform(-1,1) for _ in range(dim)];k=[rng.uniform(-1,1) for _ in range(tokens*dim)];v=[rng.uniform(-1,1) for _ in k]
            scores=[sum(q[f]*k[n*dim+f] for f in range(dim))/math.sqrt(dim) for n in range(tokens)]
            weights=[math.exp(s-max(scores)) for s in scores];den=sum(weights)
            dense=[sum(weights[n]/den*v[n*dim+f] for n in range(tokens)) for f in range(dim)]
            self.assertEqual(bank_local_attention(topo,q,k,v,tokens,dim),dense)
    def test_command_lane_coverage_traffic_and_callbacks(self):
        graph,manifest=attention_graph(self.config,17,19)
        for tensor in ("K","V"):
            elements=[tuple(x[:2]) for entry in manifest["command_elements"].values() if entry["tensor"]==tensor for x in entry["elements"]]
            self.assertEqual(sorted(elements),[(n,f) for n in range(17) for f in range(19)])
        path,summary=self.simulate("attention",self.config,graph)
        commands=records(path/"commands.csv");events=records(path/"events.csv")
        self.assertEqual(sum(r["command"]=="MACPB" for r in commands),manifest["issued_MACPB"])
        for event in events:
            tail=self.config["pim"]["softmax_tail_cycles" if event["type"]=="10" else "mac_tail_cycles"]
            self.assertEqual(int(event["result_ready_cycle"])-int(event["issue_cycle"]),tail)
            self.assertEqual(event["complete_cycle"],event["result_ready_cycle"])
        tsv=[r for r in records(path/"resources.csv") if r["resource"]=="tsv/s0"]
        self.assertEqual(sum(int(r["bytes"]) for r in tsv),sum(manifest["traffic_bytes"].values()))
        self.assertEqual(manifest["partial_reduce_bytes"],0)
    def test_result_tail_sensitivity_and_ordinary_access_exclusion(self):
        topo=Topology(self.config);die=topo.dies[4]
        graph={"schema_version":1,"tasks":[{"id":"mac","source":"pim","requests":[{"type":6,"address":die.base}]},
            {"id":"dependent_read","source":"gpu","depends_on":["mac"],"requests":[{"type":0,"address":die.base}]},
            {"id":"independent_read","source":"gpu","issue_cycle":24,"requests":[{"type":0,"address":die.base+32}]}]}
        path,fast=self.simulate("tail8",self.config,graph)
        slow=copy.deepcopy(self.config);slow["pim"]["mac_tail_cycles"]=32
        slowpath,limited=self.simulate("tail32",slow,graph)
        self.assertGreater(limited["complete_cycle"],fast["complete_cycle"])
        events=records(slowpath/"events.csv");mac=next(e for e in events if e["type"]=="6")
        commands=records(slowpath/"commands.csv")
        for command in commands:
            if command["die_id"]=="4" and command["channel"]=="0":
                self.assertFalse(int(mac["issue_cycle"])<int(command["cycle"])<int(mac["result_ready_cycle"]))
    def test_multi_head_graphs_and_bad_tail(self):
        graph1,_=attention_graph(self.config,3,7,block=0,prefix="head0")
        graph2,_=attention_graph(self.config,3,7,block=1,prefix="head1")
        graph1["tasks"]+=graph2["tasks"];self.simulate("heads",self.config,graph1)
        bad=copy.deepcopy(self.config);bad["pim"]["mac_tail_cycles"]=0
        proc=run_case(self.path/"bad-tail",bad,graph1)
        self.assertNotEqual(proc.returncode,0);self.assertIn("tail must be positive",proc.stderr)

    def test_PIM_waits_for_prior_ordinary_data_and_write_recovery(self):
        die=Topology(self.config).dies[4]
        for type_ in (0,1):
            graph={"schema_version":1,"tasks":[
                {"id":"ordinary","source":"gpu","requests":[{"type":type_,"address":die.base}]},
                {"id":"mac","source":"pim","issue_cycle":30,"requests":[{"type":6,"address":die.base}]}]}
            path,_=self.simulate(f"mode-drain-{type_}",self.config,graph)
            events=records(path/"events.csv");ordinary=next(e for e in events if e['type']==str(type_));mac=next(e for e in events if e['type']=='6')
            self.assertLess(int(ordinary['issue_cycle']),int(mac['issue_cycle']))
            self.assertGreaterEqual(int(mac['issue_cycle']),int(ordinary['complete_cycle']))

if __name__=="__main__":unittest.main()
