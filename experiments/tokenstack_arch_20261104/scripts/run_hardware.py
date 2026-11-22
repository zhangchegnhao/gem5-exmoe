"""Run the explicit TokenStack hardware fabric; preserve config/trace/raw logs."""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import resource
import math
import shutil
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hardware_topology import Topology
REPO = ROOT.parents[1]/"attacc_simulator/ramulator2"


def no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))


def records(path: Path) -> list[dict[str,str]]:
    with path.open() as stream:
        return list(csv.DictReader(stream))


def runtime_config(config: dict, output: Path) -> dict:
    topo=Topology(config)
    return {"Frontend":{"impl":"TokenStackGraph","clock_ratio":1,"max_cycles":200000,
                         "path":str(output/"graph.yaml"),"task_log":str(output/"tasks.csv"),
                         "request_log":str(output/"requests.csv"),"buffer_log":str(output/"buffers.csv"),
                         "buffer_bytes_per_group":config["base_die"]["page_tokens"]*config["base_die"]["head_dim"]*4*config["base_die"]["buffer_slots_per_group"],
                         "head_groups":config["base_die"]["head_groups"]*config["stacks"],"aging_cycles":config["resources"]["aging_cycles"]},
            "MemorySystem":{"impl":"TokenStackFabric","clock_ratio":1,
                            "layers":topo.ramulator_layers(str(output)),
                            "hardware":config,"command_log":str(output/"commands.csv"),
                            "resource_log":str(output/"resources.csv"),"event_log":str(output/"events.csv")}}


def run_case(output: Path, config: dict, graph: dict, *, timeout: int=120) -> subprocess.CompletedProcess:
    output.mkdir(parents=True,exist_ok=False)
    (output/"hardware.yaml").write_text(yaml.safe_dump(config,sort_keys=False))
    (output/"graph.yaml").write_text(yaml.safe_dump(graph,sort_keys=False))
    runtime=runtime_config(config,output)
    (output/"config.yaml").write_text(yaml.safe_dump(runtime,sort_keys=False))
    command=[str(REPO/"build/ramulator2"),"-f",str(output/"config.yaml")]
    (output/"command.json").write_text(json.dumps(command,indent=2)+"\n")
    proc=subprocess.run(command,cwd=REPO,text=True,capture_output=True,timeout=timeout,preexec_fn=no_core)
    (output/"stdout.log").write_text(proc.stdout)
    (output/"stderr.log").write_text(proc.stderr)
    return proc


def topology_graph(config: dict) -> dict:
    topo=Topology(config)
    tasks=[]
    for n,die in enumerate(topo.dies):
        # Last burst ensures full die addressability, first burst ensures routing.
        addresses=[die.base,die.base+die.size-32]
        for k,address in enumerate(addresses):
            name=f"s{die.stack}_d{die.die_id}_b{k}"
            tasks += [{"id":name+"_wr","source":"gpu","requests":[{"type":1,"address":address}]},
                      {"id":name+"_rd","source":"gpu","depends_on":[name+"_wr"],
                       "requests":[{"type":0,"address":address}]}]
        if die.kind=="compute":
            tasks.append({"id":f"pim_{n}","source":"pim","depends_on":[f"s{die.stack}_d{die.die_id}_b1_rd"],
                          "requests":[{"type":4,"address":die.base}]})
    return {"schema_version":1,"tasks":tasks}


def validate_graph(output: Path, graph: dict) -> dict:
    tasks=records(output/"tasks.csv"); requests=records(output/"requests.csv")
    inputs={node["id"]:node for node in graph["tasks"]}
    assert len(tasks)==len(inputs) and {r["task_id"] for r in tasks}==set(inputs),"task conservation"
    by_id={r["task_id"]:r for r in tasks}
    expected=[(i,op["type"],op["address"],op.get("byte_mask",0xffffffff)) for i,node in enumerate(graph["tasks"]) for op in node.get("requests",[])]
    actual=[(int(r["task_index"]),int(r["type"]),int(r["address"]),int(r["byte_mask"])) for r in requests]
    assert sorted(actual)==sorted(expected),"request/byte conservation"
    assert sorted(int(r["request_id"]) for r in requests)==list(range(len(requests))),"request IDs"
    for node in graph["tasks"]:
        row=by_id[node["id"]]
        assert int(row["issue_cycle"])<=int(row["eligible_cycle"])<=int(row["start_cycle"])<=int(row["complete_cycle"])
        for dep in node.get("depends_on",[]):
            assert int(by_id[dep]["complete_cycle"])<=int(row["start_cycle"]),"dependency completion"
    summary={"tasks":len(tasks),"requests":len(requests),
             "read_bytes":32*sum(int(r["type"])==0 for r in requests),
             "write_bytes":32*sum(int(r["type"])==1 for r in requests),
             "complete_cycle":max(int(r["complete_cycle"]) for r in tasks)}
    (output/"verification.json").write_text(json.dumps(summary,indent=2)+"\n")
    return summary


def validate_fabric(output: Path, config: dict, graph: dict) -> dict:
    summary=validate_graph(output,graph)
    reservations=records(output/"resources.csv");events=records(output/"events.csv")
    commands=records(output/"commands.csv");requests=records(output/"requests.csv")
    groups={}
    for row in reservations:
        assert int(row["start_cycle"])<int(row["end_cycle"]),"empty resource reservation"
        groups.setdefault(row["resource"],[]).append(row)
    occupancy=audit_resource_calendar(groups,config)
    assert len(events)==len(requests),"hardware completion event conservation"
    event_ids={int(r["request_id"]):r for r in events}
    assert len(event_ids)==len(events),"duplicate hardware event"
    topo=Topology(config)
    stdout=(output/"stdout.log").read_text()
    stats=yaml.safe_load(stdout[stdout.index("Frontend:"):])
    memory_stats=stats["MemorySystem"];simulation_end=memory_stats["memory_system_cycles"]
    assert simulation_end>=summary["complete_cycle"],"simulation stops before tasks complete"
    assert memory_stats["total_num_read_requests"]*32==summary["read_bytes"],"read stats conservation"
    assert memory_stats["total_num_write_requests"]*32==summary["write_bytes"],"write stats conservation"
    assert memory_stats["total_num_pim_requests"]==sum(int(r["type"])>=4 for r in requests),"PIM stats conservation"
    assert len(stats["PhysicalDies"])==len(topo.dies),"missing physical tree statistics"
    assert all(d["stats"]["memory_system_cycles"]==simulation_end for d in stats["PhysicalDies"]),"die clocks diverged"
    assert all(int(r["end_cycle"])<=simulation_end for r in reservations),"resource interval outlives simulation"
    commands_by_request={}
    for command_row in commands:
        commands_by_request.setdefault(int(command_row['request_id']),[]).append(command_row)
    memory_tsv_by_request={}
    for reservation in reservations:
        if reservation['resource'].startswith('tsv/') and int(reservation['direction']):
            memory_tsv_by_request.setdefault(int(reservation['tag']),[]).append(reservation)
    for row in requests:
        request_id=int(row["request_id"]);event=event_ids[request_id]
        assert int(event["complete_cycle"])==int(row["complete_cycle"]),"result delivery horizon"
        die=topo.find(int(row["address"]),32,int(row["type"])>=4)
        assert (die.stack,die.die_id)==(int(event["stack"]),int(event["die_id"])),"die routing"
        cmds=commands_by_request.get(request_id,[])
        if int(row["type"])<2:
            command="RD" if int(row["type"])==0 else "WR"
            final=[c for c in cmds if c["command"]==command]
            assert len(final)==1,"missing/duplicated RD/WR command"
            assert int(final[0]["cycle"])==int(event["issue_cycle"]),"command issue evidence"
            tsv=memory_tsv_by_request.get(request_id,[])
            assert len(tsv)==1 and int(tsv[0]["bytes"])==32,"physical TSV burst coverage"
            assert tsv[0]['resource']==f'tsv/s{die.stack}',"physical TSV stack"
            assert int(event["data_ready_cycle"])==int(tsv[0]["end_cycle"]),"DRAM/TSV same interval"
            assert int(event["complete_cycle"])>=int(event["data_ready_cycle"]),"data-before-completion"
            coords,_=die.decode(int(row["address"]))
            assert tuple(int(final[0][a]) for a in ("channel","pseudochannel","rank","bankgroup","bank","row","column"))==coords,"address decode"
        assert int(event["accept_cycle"])<=int(event["issue_cycle"])<=int(event["complete_cycle"]),"hardware time order"
    tsv_bytes=sum(int(r["bytes"]) for r in reservations if r["resource"].startswith("tsv/"))
    memory_tsv_bytes=sum(int(r["bytes"]) for r in reservations if r["resource"].startswith("tsv/") and int(r["direction"])!=0)
    assert memory_tsv_bytes==summary["read_bytes"]+summary["write_bytes"],"no extra or missing migration hop bytes"
    allocations={};used={};peak={};buffer_rows=records(output/"buffers.csv")
    capacity=config["base_die"]["page_tokens"]*config["base_die"]["head_dim"]*4*config["base_die"]["buffer_slots_per_group"]
    for row in buffer_rows:
        group=int(row["head_group"]);size=int(row["bytes"]);page=row["page_id"]
        if row["event"]=="allocate":
            assert page not in allocations,"double page allocation"
            allocations[page]=(group,size);used[group]=used.get(group,0)+size
        else:
            assert allocations.pop(page)==(group,size),"release without matching page"
            used[group]-=size
        assert used[group]==int(row["occupied_bytes"]) and 0<=used[group]<=capacity,"buffer occupancy conservation"
        peak[group]=max(peak.get(group,0),used[group])
    assert not allocations and all(x==0 for x in used.values()),"buffer leak"
    summary["buffer_peak_bytes_by_stack_head_group"]=peak
    summary.update(tsv_bytes=tsv_bytes,command_count=len(commands),hardware_events=len(events),resource_occupancy=occupancy,
                   refresh_commands=sum(c["command"].startswith("REF") for c in commands),
                   cycles_per_second=10**12/config["clock_ps"],simulation_end_cycle=simulation_end,
                   maintenance_drain_cycles=simulation_end-summary["complete_cycle"])
    for name,row in occupancy.items():
        row["utilization"]=row["busy_cycles"]/simulation_end
        row["effective_GBps"]=row["bytes"]/(simulation_end*config["clock_ps"])*1000
        if name.startswith('tsv/'):
            row['bandwidth_utilization']=row['effective_GBps']/config['resources']['tsv_GBps']
    latencies=sorted(int(r["complete_cycle"])-int(r["accept_cycle"]) for r in requests)
    summary["request_latency_cycles"]={"mean":sum(latencies)/len(latencies) if latencies else None,
        "p50":latencies[math.ceil(.5*len(latencies))-1] if latencies else None,
        "p95":latencies[math.ceil(.95*len(latencies))-1] if latencies else None}
    (output/"verification.json").write_text(json.dumps(summary,indent=2)+"\n")
    return summary


def audit_resource_calendar(groups: dict,config: dict) -> dict:
    """Independent sweep of piecewise-constant byte rates and direction guards.

    TSV busy time is interval UNION, not a sum of overlapping reservations.
    Direction 0 denotes coordinator traffic with unspecified physical direction;
    it consumes bandwidth but cannot erase outstanding RD/WR turnaround guards.
    """
    occupancy={}
    for name,rows in groups.items():
        rows=sorted(rows,key=lambda r:int(r['start_cycle']))
        shared=name.startswith('tsv/')
        changes={};ends={1:0,2:0};previous_end=0
        for row in rows:
            start,end=int(row['start_cycle']),int(row['end_cycle'])
            size,direction=int(row['bytes']),int(row['direction'])
            assert start>=0 and end>start and size>=0 and direction in (0,1,2),'invalid reservation'
            if direction:
                other=ends[3-direction]
                assert not other or other+config['resources']['tsv_turnaround_cycles']<=start,f'overlap/turnaround {name}'
                ends[direction]=max(ends[direction],end)
            if not shared:assert previous_end<=start,f'overlap/turnaround {name}'
            previous_end=max(previous_end,end)
            rate=size/(end-start)
            for cycle,count,delta in ((start,1,rate),(end,-1,-rate)):
                old=changes.get(cycle,(0,0.0));changes[cycle]=(old[0]+count,old[1]+delta)
        count=0;rate=0.0;peak=0.0;busy=0;last=0
        for cycle,(delta,byte_rate) in sorted(changes.items()):
            if count:busy+=cycle-last
            count+=delta;rate+=byte_rate;peak=max(peak,rate);last=cycle
            if shared:
                assert rate<=config['resources']['tsv_GBps']*config['clock_ps']/1000+1e-8,f'TSV bandwidth exceeded {name}'
        assert count==0,'unbalanced resource intervals'
        occupancy[name]={'busy_cycles':busy,'bytes':sum(int(r['bytes']) for r in rows),'peak_bytes_per_cycle':peak}
    return occupancy


def snapshot(output: Path) -> None:
    hashes={}
    paths=list(ROOT.glob('*.py'))+[Path(__file__),
           REPO/"build/ramulator2",REPO/"libramulator.so",REPO/"build/CMakeCache.txt"]
    for name in ("scripts","tests","config","spec"):
        paths += [p for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    paths += list((REPO/"src").rglob("*tokenstack*"))
    paths += [REPO/"src/dram/impl/HBM3-PIM.cpp",REPO/"src/dram_controller/impl/hbm3_pim_controller.cpp"]
    paths += [REPO/"src/dram/lambdas/preq.h",REPO/"src/dram/lambdas/action.h"]
    paths += [REPO.parent/name for name in ('src/config.py','src/model.py','src/type.py','LICENSE')]
    for path in paths:
        if not path.is_file(): continue
        relative=path.relative_to(ROOT.parents[1])
        hashes[str(relative)]=hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix not in (".so",) and path.name!="ramulator2":
            target=output/"sources"/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    metadata={"sha256":hashes,"python":sys.version,"pyyaml":yaml.__version__,"seed":260505639,"argv":sys.argv,
              "model_precision":"DRAM command timing + Base Die resource behavior; engineering profile",
              "repos":{}}
    for name,path in (("attacc",REPO.parent),("ramulator",REPO),("gem5",ROOT.parents[1]/"gem5-src")):
        if not path.exists():
            metadata["repos"][name]={"available":False,"note":"not present in source archive environment"}
            continue
        metadata["repos"][name]={key:subprocess.run(["git",*args],cwd=path,text=True,capture_output=True).stdout
                                  for key,args in (("head",["rev-parse","HEAD"]),("status",["status","--short"]))}
    (output/"metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("run_id")
    parser.add_argument("--stacks",type=int,default=1);args=parser.parse_args()
    if Path(args.run_id).name!=args.run_id or args.run_id in (".",".."):parser.error("new simple run ID required")
    config=yaml.safe_load((ROOT/"config/tokenstack_hardware.yaml").read_text());config["stacks"]=args.stacks
    graph=topology_graph(config);output=ROOT/"results"/args.run_id
    proc=run_case(output,config,graph);proc.check_returncode()
    summary=validate_fabric(output,config,graph);snapshot(output)
    print(json.dumps(summary,indent=2))


if __name__=="__main__":main()
