"""Saturated row-hit traces with explicit scope, queue provisioning and ceilings."""
from __future__ import annotations
import copy
from hardware_topology import Topology, check


def read_stream(config: dict, channels: int=16, dies: int=1, bursts: int=64) -> dict:
    topo=Topology(config);tasks=[]
    check(channels in (1,4,16) and dies in (1,4) and bursts>0,'invalid saturation shape')
    for stack in range(config['stacks']):
        selected=[d for d in topo.dies if d.stack==stack and d.kind=='capacity'][:dies]
        check(len(selected)==dies,'insufficient Capacity dies')
        for die in selected:
            for channel in range(channels):
                for pc in range(2):
                    for bg in range(4):
                        tasks.append({'id':f's{stack}d{die.die_id}c{channel}p{pc}b{bg}',
                            'source':'gpu','requests':[{'type':0,'address':die.encode((channel,pc,0,bg,0,0,n%32))}
                                                     for n in range(bursts)]})
    return {'schema_version':1,'tasks':tasks}


def provision(config: dict, bandwidth: float=896, stacks: int=1) -> dict:
    effective=copy.deepcopy(config);effective['stacks']=stacks
    # Synthetic saturated ingress; not paper-specified controller queue sizes.
    effective['resources'].update(tsv_GBps=bandwidth,queue_depth_per_source=256,dispatch_per_source_cycle=16)
    return effective


def dram_ceiling_GBps(config: dict,channels: int=16) -> float:
    # Inherited nBL=2, two PCs and one command slot/channel/cycle. Die count
    # cannot multiply shared PC/Channel throughput. Profile loader fixes clock.
    check(config['dram']['timing']=='HBM3_5.2Gbps','re-audit nBL for another preset')
    return channels*2*32/(2*config['clock_ps'])*1000
