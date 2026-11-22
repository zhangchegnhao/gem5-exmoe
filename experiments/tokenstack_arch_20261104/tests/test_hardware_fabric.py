"""Live Ramulator integration and negative-input tests for the new fabric."""
from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from run_hardware import run_case,topology_graph,validate_graph,validate_fabric,records

class FabricTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="tokenstack-fabric-");self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text())
    def run_graph(self,name,graph,config=None):
        path=self.output/name
        proc=run_case(path,config or self.config,graph)
        self.assertEqual(proc.returncode,0,proc.stderr)
        validate_fabric(path,config or self.config,graph)
        return path
    def test_all_dies_first_last_burst_and_PIM(self):
        path=self.run_graph("topology",topology_graph(self.config))
        self.assertEqual(len(records(path/"requests.csv")),36)
    def test_missing_refresh_and_capacity_PIM_rejected(self):
        graph={"schema_version":1,"tasks":[{"id":"bad","source":"pim","requests":[{"type":4,"address":0}]}]}
        proc=run_case(self.output/"cap-pim",self.config,graph)
        self.assertNotEqual(proc.returncode,0);self.assertIn("PIM forbidden",proc.stderr)
        for key in ("nRFC","nREFISB"):
            config=copy.deepcopy(self.config);del config["dram"][key]
            # Build runtime config explicitly because high-level loader requires fields.
            from run_hardware import runtime_config,REPO,no_core
            import subprocess
            runtime=runtime_config(self.config,self.output)
            del runtime["MemorySystem"]["layers"][0]["backend"]["DRAM"]["timing"][key]
            trace=self.output/"graph.yaml";trace.write_text(yaml.safe_dump(graph))
            cfg=self.output/(key+".yaml");cfg.write_text(yaml.safe_dump(runtime))
            process=subprocess.run([str(REPO/"build/ramulator2"),"-f",str(cfg)],capture_output=True,text=True,preexec_fn=no_core)
            self.assertNotEqual(process.returncode,0)
            self.assertIn("unsupported density requires explicit",process.stderr)
    def test_graph_missing_dependency_cycle_bad_address_rejected(self):
        bad=[{"id":"bad","source":"gpu","depends_on":["missing"]},
             {"id":"bad","source":"gpu","depends_on":["bad"]},
             {"id":"bad","source":"gpu","requests":[{"type":0,"address":1}]},
             {"id":"bad","source":"gpu","requests":[{"type":0,"address":12*2**30}]}]
        for i,node in enumerate(bad):
            proc=run_case(self.output/f"bad-{i}",self.config,{"schema_version":1,"tasks":[node]})
            self.assertNotEqual(proc.returncode,0,proc.stdout)

    def test_shared_command_tsv_and_independent_channels(self):
        from hardware_topology import Topology
        topo=Topology(self.config);tasks=[]
        for die in topo.dies[:4]:
            for ch in range(2):
                address=die.encode((ch,0,0,0,0,0,0))
                tasks.append({"id":f"d{die.die_id}_ch{ch}","source":"promotion" if ch else "gpu",
                              "requests":[{"type":0,"address":address+32*i} for i in range(12)]})
        graph={"schema_version":1,"tasks":tasks};path=self.run_graph("shared",graph)
        commands=records(path/"commands.csv")
        cycles={}
        for cmd in commands:cycles.setdefault(cmd["cycle"],set()).add(cmd["channel"])
        self.assertTrue(any(len(channels)>1 for channels in cycles.values()),"channels accidentally serialized")
        self.assertEqual(sum(int(r["bytes"]) for r in records(path/"resources.csv") if r["resource"]=="tsv/s0"),96*32)

    def test_row_hit_conflict_and_refresh_progress(self):
        from hardware_topology import Topology
        topo=Topology(self.config);die=topo.dies[0];tasks=[]
        previous=[]
        for i,(row,col) in enumerate([(0,0),(0,1),(1,0)]+[(n%2,n%32) for n in range(20)]):
            name=f"r{i}";tasks.append({"id":name,"source":"gpu","depends_on":previous,
                                     "requests":[{"type":0,"address":die.encode((0,0,0,0,0,row,col))}]});previous=[name]
        config=copy.deepcopy(self.config);config["dram"].update(nREFI=200,nRFC=32)
        path=self.run_graph("refresh",{"schema_version":1,"tasks":tasks},config)
        commands=records(path/"commands.csv")
        self.assertTrue(any(c["command"].startswith("REF") for c in commands))
        self.assertTrue(any(c["command"]=="PRE" for c in commands))
        hits=[c for c in commands if c["request_id"]=="1"]
        self.assertEqual([c["command"] for c in hits],["RD"])

    def test_broadcast_checks_all_banks_before_ACT(self):
        from hardware_topology import Topology
        die=Topology(self.config).dies[4]
        # First bank closed, a later broadcast target open on another row:
        # the inherited early-return precondition used to issue ACT over it.
        for type_,bank,bg in [(4,1,0),(5,0,1),(6,0,0)]:
            pc=1 if type_==6 else 0
            graph={"schema_version":1,"tasks":[
                {"id":"open_later","source":"gpu","requests":[{"type":0,"address":die.encode((0,pc,0,bg,bank,0,0))}]},
                {"id":"pim","source":"pim","depends_on":["open_later"],"requests":[{"type":type_,"address":die.encode((0,0,0,0,0,1,0))}]}]}
            path=self.run_graph(f"broadcast-{type_}",graph)
            cmds=[c["command"] for c in records(path/"commands.csv") if c["request_id"]=="1"]
            self.assertTrue(cmds[0].startswith('PRE'),cmds)

    def test_slow_TSV_write_recovery_follows_actual_data_end(self):
        config=copy.deepcopy(self.config);config['resources']['tsv_GBps']=1
        graph={'schema_version':1,'tasks':[{'id':'wr','source':'gpu','requests':[{'type':1,'address':0}]}]}
        path=self.run_graph('slow-write',graph,config)
        event=records(path/'events.csv')[0]
        self.assertEqual(int(event['complete_cycle'])-int(event['data_ready_cycle']),21)

if __name__=="__main__":unittest.main()
