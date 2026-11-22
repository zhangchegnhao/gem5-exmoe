"""Attention command/traffic compiler and independent bank-local CPU reference."""
from __future__ import annotations
from collections import defaultdict
import math
from hardware_topology import Topology,check


def attention_graph(config: dict, tokens: int,dim: int, *,stack: int=0,block: int=0,
                    prefix: str="attention",depends_on: list[str]|None=None,issue: int=0) -> tuple[dict,dict]:
    topo=Topology(config);check(tokens>0 and dim>0,"invalid attention shape")
    tasks=[];groups={};logical_maps={};traffic={};previous=depends_on or []
    for tensor,phase in (("K","qk"),("V","av")):
        # AttAcc MACPB broadcasts across both PCs. Coalesce equal command
        # addresses, retaining explicit logical valid lanes; idle PC results
        # are discarded rather than counted as extra tensor elements.
        bursts=defaultdict(list)
        for n in range(tokens):
            for f in range(dim):
                address=topo.kv_address(stack,tensor,n,f,tokens,dim,block)
                die=topo.find(address);coords,_=die.decode(address//32*32)
                key=(die.die_id,coords[0],coords[2],coords[3],coords[4],coords[5],coords[6])
                bursts[key].append((n,f,coords[1]))
        groups[phase]=bursts
    q_channels={(key[0],key[1]) for key in groups["qk"]}
    av_channels={(key[0],key[1]) for key in groups["av"]}
    q_bytes=dim*2*len(q_channels);weight_bytes=tokens*2*len(av_channels)
    broadcast=prefix+"_query_broadcast"
    tasks.append({"id":broadcast,"source":"pim","issue_cycle":issue,"depends_on":previous,
                  "resource":"coordinator","stack":stack,"bytes":q_bytes,"cycles":0})
    previous=[broadcast]
    for phase in ("qk","av"):
        commands=[];chains={}
        for i,(key,elements) in enumerate(sorted(groups[phase].items())):
            die_id,ch,rank,bg,bank,row,col=key
            die=next(d for d in topo.dies if d.stack==stack and d.die_id==die_id)
            address=die.encode((ch,0,rank,bg,bank,row,col))
            name=f"{prefix}_{phase}_{i}";chain=(die_id,ch,rank,bg,bank)
            dependencies=list(previous)
            if chain in chains:dependencies.append(chains[chain])
            tasks.append({"id":name,"source":"pim","depends_on":dependencies,
                          "requests":[{"type":6,"address":address}]})
            chains[chain]=name;commands.append(name)
            logical_maps[name]={"tensor":"K" if phase=="qk" else "V","elements":[list(x) for x in elements],
                                "address":address,"broadcast_pcs":2,"valid_pcs":sorted({x[2] for x in elements})}
        gather=prefix+"_"+phase+"_gather"
        size=tokens*2 if phase=="qk" else dim*2
        tasks.append({"id":gather,"source":"pim","depends_on":commands,"resource":"coordinator",
                      "stack":stack,"bytes":size,"cycles":0})
        previous=[gather]
        if phase=="qk":
            marker=prefix+"_softmax"
            first=next(d for d in topo.dies if d.stack==stack and d.kind=="compute")
            tasks.append({"id":marker,"source":"pim","depends_on":previous,"requests":[{"type":10,"address":first.base}]})
            weight=prefix+"_weights_broadcast"
            tasks.append({"id":weight,"source":"pim","depends_on":[marker],"resource":"coordinator",
                          "stack":stack,"bytes":weight_bytes,"cycles":0})
            previous=[weight]
    done=prefix+"_done";tasks.append({"id":done,"source":"pim","depends_on":previous})
    traffic={"query_broadcast":q_bytes,"score_gather":tokens*2,"weight_broadcast":weight_bytes,"output_gather":dim*2}
    return {"schema_version":1,"tasks":tasks},{"schema_version":1,"tokens":tokens,"dim":dim,
        "logical_MACs":2*tokens*dim,"issued_MACPB":len(logical_maps),"command_elements":logical_maps,
        "traffic_bytes":traffic,"partial_reduce_bytes":0,"done_task":done,
        "softmax_placement":"Base Die behavior with AttAcc SFM timing marker; configured uncalibrated tail",
        "inactive_PC_policy":"broadcast computes are timing-accounted; inactive logical lanes discarded",
        "pim_unit_occupancy_scope":config["pim"]["result_scope"]}


def bank_local_attention(topo: Topology,query: list[float],keys: list[float],values: list[float],tokens: int,dim: int) -> list[float]:
    check(len(query)==dim and len(keys)==len(values)==tokens*dim,"attention tensor sizes")
    sparse={}
    for tensor,data in (("K",keys),("V",values)):
        for n in range(tokens):
            for f in range(dim):sparse[topo.kv_address(0,tensor,n,f,tokens,dim)]=data[n*dim+f]
    scores=[]
    for n in range(tokens):
        addresses=[topo.kv_address(0,"K",n,f,tokens,dim) for f in range(dim)]
        bank={(topo.find(a).die_id,topo.find(a).decode(a)[0][:5]) for a in addresses}
        check(len(bank)==1,"QK contraction crosses banks")
        scores.append(sum(query[f]*sparse[a] for f,a in enumerate(addresses))/math.sqrt(dim))
    weights=[math.exp(s-max(scores)) for s in scores];total=sum(weights);weights=[w/total for w in weights]
    output=[]
    for f in range(dim):
        addresses=[topo.kv_address(0,"V",n,f,tokens,dim) for n in range(tokens)]
        bank={(topo.find(a).die_id,topo.find(a).decode(a)[0][:5]) for a in addresses}
        check(len(bank)==1,"AV contraction crosses banks")
        output.append(sum(weights[n]*sparse[a] for n,a in enumerate(addresses)))
    return output
