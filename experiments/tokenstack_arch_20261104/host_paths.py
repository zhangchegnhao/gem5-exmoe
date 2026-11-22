"""Explicit GPU host ports and crossbar paths; no implicit Stack-to-Stack link."""
from __future__ import annotations
from hardware_topology import Topology,check


def gpu_access(config: dict,address: int, *,write: bool=False,bytes_: int=32,prefix: str="gpu",
               depends_on: list[str]|None=None,issue: int=0) -> dict:
    topo=Topology(config);die=topo.find(address,bytes_)
    check(address%32==0 and bytes_>0 and bytes_%32==0,"GPU access burst alignment")
    ingress=prefix+"_ingress";memory=prefix+"_memory";egress=prefix+"_egress"
    tasks=[{"id":ingress,"source":"gpu","issue_cycle":issue,"depends_on":depends_on or [],
            "resource":"ucie","stack":die.stack,"bytes":bytes_ if write else 0,"cycles":1},
           {"id":memory,"source":"gpu","depends_on":[ingress],
            "requests":[{"type":int(write),"address":address+n} for n in range(0,bytes_,32)]},
           {"id":egress,"source":"gpu","depends_on":[memory],"resource":"ucie","stack":die.stack,
            "bytes":0 if write else bytes_,"cycles":1}]
    return {"schema_version":1,"tasks":tasks}


def cross_stack_copy(config: dict,source: int,target: int,bytes_: int, *,prefix: str="remote",
                     kind: str="promotion",depends_on: list[str]|None=None,source_card: int=0,target_card: int=0) -> tuple[dict,dict]:
    check(source_card==target_card==0,"NVLink external boundary is unconfigured; cross-card request rejected")
    topo=Topology(config);src=topo.find(source,bytes_);dst=topo.find(target,bytes_)
    check(src.stack!=dst.stack,"cross-stack copy requires different stacks")
    check(bytes_>0 and bytes_%32==0 and source%32==target%32==0,"remote copy burst alignment")
    check(kind in ("promotion","demotion"),"invalid remote source")
    page_bytes=config["base_die"]["page_tokens"]*config["base_die"]["head_dim"]*4
    check(page_bytes%32==0,"remote page must be burst aligned")
    tasks=[];commits=[]
    for page,offset in enumerate(range(0,bytes_,page_bytes)):
        count=min(page_bytes,bytes_-offset);name=f"{prefix}_p{page}";previous=depends_on or []
        # Canonical stack order prevents opposing transfers acquiring buffers
        # in a cycle. Both source and destination SRAM lifetimes are bounded.
        allocations=[]
        for stack in sorted((src.stack,dst.stack)):
            allocation=f"{name}_buffer_s{stack}";allocations.append(allocation)
            tasks.append({"id":allocation,"source":kind,"depends_on":previous,"acquire":allocation,
                          "buffer_group":stack*config["base_die"]["head_groups"],"buffer_bytes":page_bytes})
            previous=[allocation]
        for suffix,resource,stack,cycles in (("read",None,src.stack,0),("source_egress","ucie",src.stack,0),
                ("host_forward","host",src.stack,config["resources"]["host_forward_cycles"]),
                ("target_ingress","ucie",dst.stack,0),("write",None,dst.stack,0)):
            task_id=name+"_"+suffix
            node={"id":task_id,"source":kind,"depends_on":previous}
            if resource:node.update(resource=resource,stack=stack,bytes=count,cycles=cycles)
            else:node["requests"]=[{"type":int(suffix=="write"),"address":(source if suffix=="read" else target)+offset+n} for n in range(0,count,32)]
            tasks.append(node);previous=[task_id]
        for allocation in allocations:
            release=allocation+"_release"
            tasks.append({"id":release,"source":kind,"depends_on":previous,"release":allocation})
            commits.append(release)
    tasks.append({"id":prefix+"_done","source":kind,"depends_on":commits})
    return {"schema_version":1,"tasks":tasks},{"schema_version":1,"bytes":bytes_,"source_stack":src.stack,"target_stack":dst.stack,
        "path":["source_dram_and_tsv","source_ucie","gpu_crossbar","target_ucie","target_dram_and_tsv"],
        "expected_resource_bytes":{f"tsv/s{src.stack}":bytes_,f"ucie/s{src.stack}":bytes_,"gpu_crossbar":bytes_,
                                   f"ucie/s{dst.stack}":bytes_,f"tsv/s{dst.stack}":bytes_},
        "direct_stack_link":False,"nvlink_mode":"reject_unconfigured"}
