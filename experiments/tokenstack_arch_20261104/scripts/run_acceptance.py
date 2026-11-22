"""H1-H6 engineering acceptance matrix; paper-fidelity status remains separate."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import yaml
from run_hardware import ROOT,run_case,validate_fabric,snapshot,topology_graph,records
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from attention_hardware import attention_graph
from hardware_topology import Topology
from host_paths import cross_stack_copy
from kv_pipeline import migration_graph,functional_roundtrip
from test_hardware_integration import mixed_graph


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id')
    parser.add_argument('--profile',choices=['tokenstack_smoke','tokenstack_paper_profile'],default='tokenstack_smoke')
    parser.add_argument('--allow-engineering-assumptions',action='store_true');args=parser.parse_args()
    if Path(args.run_id).name!=args.run_id or args.run_id in ('.','..'):parser.error('new run ID required')
    if args.profile=='tokenstack_paper_profile' and not args.allow_engineering_assumptions:
        parser.error('paper fidelity is not frozen; use --allow-engineering-assumptions to run the declared five-stack candidate')
    output=ROOT/'results'/args.run_id;output.mkdir(exist_ok=False)
    original=yaml.safe_load((ROOT/'config/tokenstack_hardware.yaml').read_text());cases=[]
    for stacks in (1,2,5):
        config=copy.deepcopy(original);config['stacks']=stacks
        cases.append((f'topology-{stacks}',config,topology_graph(config),{}))
    for kind in ('promotion','demotion'):
        config=copy.deepcopy(original);graph,manifest=migration_graph(config,tokens=37,kind=kind)
        cases.append((f'large-{kind}',config,graph,manifest))
    for rate in (896,1):
        config=copy.deepcopy(original);config['base_die']['conversion_GBps']=rate
        graph,manifest=migration_graph(config,tokens=12);cases.append((f'convert-{rate}',config,graph,manifest))
    for tail in (1,8,32):
        config=copy.deepcopy(original);config['pim']['mac_tail_cycles']=tail
        graph,manifest=attention_graph(config,17,19);cases.append((f'tail-{tail}',config,graph,manifest))
    for stacks in (1,2,5):
        config=copy.deepcopy(original);config['stacks']=stacks;config['resources']['queue_depth_per_source']=1
        config['dram'].update(nREFI=300,nRFC=32)
        cases.append((f'mixed-refresh-{stacks}',config,mixed_graph(config),{'seed':config['seed'],'refresh_timings':'synthetic stress profile'}))
    for rate in (2560,1):
        config=copy.deepcopy(original);config['stacks']=2;config['resources']['host_crossbar_GBps']=rate
        graph,manifest=cross_stack_copy(config,0,12*2**30,256);cases.append((f'cross-stack-host-{rate}',config,graph,manifest))
    reports={}
    for name,config,graph,manifest in cases:
        path=output/name;proc=run_case(path,config,graph);proc.check_returncode()
        reports[name]=validate_fabric(path,config,graph)
        (path/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    assert reports['convert-1']['complete_cycle']>reports['convert-896']['complete_cycle']
    assert reports['tail-32']['complete_cycle']>reports['tail-8']['complete_cycle']>reports['tail-1']['complete_cycle']
    assert reports['cross-stack-host-1']['complete_cycle']>reports['cross-stack-host-2560']['complete_cycle']
    assert all(reports[f'mixed-refresh-{n}']['refresh_commands']>0 for n in (1,2,5))
    # Independently validate the numerical transport/layout reference.
    import random
    rng=random.Random(original['seed']);tokens,dim=9,13
    numerical=functional_roundtrip(original,tokens,dim,[rng.uniform(-2,2) for _ in range(tokens*dim)],
                                   [rng.uniform(-2,2) for _ in range(tokens*dim)])
    (output/'functional-reference.json').write_text(json.dumps(numerical,indent=2)+'\n')
    command=['python3','-m','unittest','discover','-s',str(ROOT/'tests'),'-p','test_*.py','-v']
    with (output/'tests.log').open('w') as stream:
        proc=subprocess.run(command,cwd=ROOT.parents[1],stdout=stream,stderr=subprocess.STDOUT)
    proc.check_returncode()
    command=['python3',str(ROOT/'scripts/verify_ramulator_regression.py'),args.run_id+'-attacc']
    with (output/'baseline.log').open('w') as stream:subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
    for script,suffix in (('run_throughput.py','throughput'),('run_gpu_system.py','gpu')):
        command=['python3',str(ROOT/'scripts'/script),args.run_id+'-'+suffix]
        with (output/(suffix+'.log')).open('w') as stream:
            subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
    snapshot(output)
    report={'schema_version':1,'engineering_acceptance':'passed','paper_fidelity_acceptance':'blocked_Q1_to_Q5',
            'paper_spec_frozen':False,'requested_profile':args.profile,'cases':reports,
            'unit_tests_log':'tests.log','baseline_run':'../'+args.run_id+'-attacc',
            'throughput_run':'../'+args.run_id+'-throughput','gpu_system_run':'../'+args.run_id+'-gpu',
            'calibration':'internal command/resource invariants and AttAcc regression only; no silicon/RTL or paper-throughput calibration'}
    (output/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({name:{key:value[key] for key in ('requests','read_bytes','write_bytes','complete_cycle','refresh_commands')} for name,value in reports.items()},indent=2))


if __name__=='__main__':main()
