from __future__ import annotations
import copy
from pathlib import Path
import random
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"scripts"))
from kv_pipeline import migration_graph,functional_roundtrip,quantize,dequantize,page_sizes
from run_hardware import run_case,validate_fabric,records

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text())
        self.temp=tempfile.TemporaryDirectory(prefix="tokenstack-page-");self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)
    def simulate(self,name,config,graph):
        output=self.output/name;proc=run_case(output,config,graph)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return output,validate_fabric(output,config,graph)
    def test_codec_layout_and_inverse_gather_nondivisible_shapes(self):
        rng=random.Random(260505639)
        for tokens,dim in [(3,7),(9,13),(20,16)]:
            keys=[rng.uniform(-2,2) for _ in range(tokens*dim)];values=[rng.uniform(-3,3) for _ in keys]
            out=functional_roundtrip(self.config,tokens,dim,keys,values)
            self.assertEqual(out["stored_elements"],2*tokens*dim)
            self.assertLess(out["max_quantization_error"],0.23)
            self.assertEqual(len(out["keys"]),tokens*dim)
    def test_codec_zero_and_metadata_rejection(self):
        for bits in (4,8):
            data,scales=quantize([0.0]*17,bits,16)
            self.assertEqual(dequantize(data,17,bits,16),[0.0]*17)
            with self.assertRaises(ValueError):dequantize(data[:-1],17,bits,16)
    def test_large_block_bounded_buffer_and_commit(self):
        graph,manifest=migration_graph(self.config,tokens=37)
        output,summary=self.simulate("large",self.config,graph)
        self.assertEqual(summary["read_bytes"],manifest["read_bytes"])
        self.assertEqual(summary["write_bytes"],manifest["write_bytes"])
        buffers=records(output/"buffers.csv")
        peak=max(int(r["occupied_bytes"]) for r in buffers)
        self.assertLessEqual(peak,512);self.assertGreater(manifest["logical_bytes"],peak)
        self.assertEqual(len([r for r in buffers if r["event"]=="allocate"]),10)
        self.assertEqual(int(buffers[-1]["occupied_bytes"]),0)
        tasks={r["task_id"]:r for r in records(output/"tasks.csv")}
        for page in manifest["pages"]:
            name=page["page_id"]
            self.assertGreaterEqual(int(tasks[name+"_commit"]["complete_cycle"]),int(tasks[name+"_write"]["complete_cycle"]))
            self.assertEqual(page["hot_valid_bytes"],page["sizes"]["fp16_payload"])
    def test_both_directions_compression_metadata_and_padding(self):
        for kind in ("promotion","demotion"):
            graph,manifest=migration_graph(self.config,tokens=9,kind=kind)
            _,summary=self.simulate(kind,self.config,graph)
            self.assertEqual(summary["read_bytes"],manifest["read_bytes"])
            self.assertEqual(summary["write_bytes"],manifest["write_bytes"])
            self.assertGreater(sum(p["sizes"]["metadata"] for p in manifest["pages"]),0)
    def test_pipeline_overlap_and_converter_bottleneck(self):
        graph,_=migration_graph(self.config,tokens=12)
        output,fast=self.simulate("fast",self.config,graph)
        tasks={r["task_id"]:r for r in records(output/"tasks.csv")}
        # Two pages resident: later-page RD starts before first-page commit.
        self.assertLess(int(tasks["move_p1_rd0"]["start_cycle"]),int(tasks["move_p0_commit"]["complete_cycle"]))
        resources=records(output/"resources.csv")
        converts=[r for r in resources if r["resource"]=="convert/s0"]
        reads=[r for r in resources if r["resource"]=="tsv/s0" and r["direction"]=="1"]
        self.assertTrue(any(int(c["start_cycle"])<int(r["end_cycle"]) and int(r["start_cycle"])<int(c["end_cycle"]) for c in converts for r in reads),"no streaming overlap")
        slow=copy.deepcopy(self.config);slow["base_die"]["conversion_GBps"]=1
        _,limited=self.simulate("slow",slow,graph)
        self.assertGreater(limited["complete_cycle"],fast["complete_cycle"])

if __name__=="__main__":unittest.main()
