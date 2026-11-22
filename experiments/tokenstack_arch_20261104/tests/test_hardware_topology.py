from __future__ import annotations
import copy
from pathlib import Path
import random
import sys
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hardware_topology import Topology

class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text())
        self.topo=Topology(self.config)
    def test_capacity_and_random_roundtrip_no_alias(self):
        rng=random.Random(self.config["seed"])
        self.assertEqual(self.topo.total_bytes,12*2**30)
        self.assertEqual(len(self.topo.dies),8)
        addresses=set()
        for die in self.topo.dies:
            for offset in [0,die.size-1]+[rng.randrange(die.size) for _ in range(1000)]:
                address=die.base+offset;coords,byte=die.decode(address)
                self.assertEqual(die.encode(coords,byte),address)
                self.assertIs(self.topo.find(address),die)
                addresses.add(address)
        self.assertGreater(len(addresses),7900)
    def test_K_and_V_reductions_are_bank_local(self):
        for tokens,dim in [(3,7),(17,19),(1031,17)]:
            for tensor in ("K","V"):
                seen=set()
                for outer in range(tokens if tensor=="K" else dim):
                    banks=set()
                    for inner in range(dim if tensor=="K" else tokens):
                        n,f=(outer,inner) if tensor=="K" else (inner,outer)
                        address=self.topo.kv_address(0,tensor,n,f,tokens,dim)
                        self.assertNotIn(address,seen);seen.add(address)
                        die=self.topo.find(address);coords,_=die.decode(address)
                        banks.add((die.die_id,coords[:5]))
                    self.assertEqual(len(banks),1)
    def test_K_V_block_padding_disjoint(self):
        sets=[]
        for block in range(2):
            sets.append({self.topo.kv_address(0,t,n,f,5,13,block) for t in ("K","V") for n in range(5) for f in range(13)})
        self.assertEqual(len(sets[0]),130);self.assertTrue(sets[0].isdisjoint(sets[1]))
        different={self.topo.kv_address(0,t,n,f,9,17,1) for t in ("K","V") for n in range(9) for f in range(17)}
        self.assertTrue(sets[0].isdisjoint(different))
    def test_cold_pages_interleave_die_fastest_then_bank(self):
        locations=[]
        total=sum(d.banks for d in self.topo.dies if d.kind=="capacity")
        for page in range(total+1):
            address=self.topo.cold_page(0,page,128);die=self.topo.find(address)
            coords,_=die.decode(address);locations.append((die.die_id,coords[:5]))
        self.assertEqual([x[0] for x in locations[:8]],[0,1,2,3,0,1,2,3])
        self.assertEqual(len(set(locations[:-1])),total)
        self.assertEqual(locations[0],locations[-1])
        self.assertNotEqual(self.topo.cold_page(0,0,128),self.topo.cold_page(0,total,128))
    def test_invalid_geometry_capacity_pim_and_bounds(self):
        for key,value in [("geometry",[16,2,1,4,4,4000,32]),("bytes",1)]:
            config=copy.deepcopy(self.config);config["dies"][0][key]=value
            with self.assertRaises(ValueError):Topology(config)
        with self.assertRaisesRegex(ValueError,"PIM forbidden"):self.topo.find(0,pim=True)
        for address,length in [(-1,32),(self.topo.total_bytes,1),(self.topo.dies[0].size-16,32)]:
            with self.assertRaises(ValueError):self.topo.find(address,length)
    def test_five_stacks_global_capacity(self):
        self.config["stacks"]=5;topo=Topology(self.config)
        self.assertEqual(topo.total_bytes,60*2**30)
        self.assertEqual(len({(d.stack,d.die_id) for d in topo.dies}),40)
        for die in topo.dies:self.assertEqual(topo.find(die.base).stack,die.stack)

if __name__=="__main__":unittest.main()
