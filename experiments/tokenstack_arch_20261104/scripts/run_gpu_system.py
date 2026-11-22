"""Reproducible A100a + five-stack timing/traffic acceptance, not a full LLM."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import yaml
from run_hardware import ROOT, records, run_case, snapshot, validate_fabric
from gpu_system import load_system, configure_system, compile_workload


def validate_system(output: Path, config: dict, graph: dict, manifest: dict) -> dict:
    hardware = validate_fabric(output,config,graph)
    tasks = {r['task_id']:r for r in records(output/'tasks.csv')}
    resources = records(output/'resources.csv')
    by_resource = {}
    for row in resources:by_resource.setdefault((int(row['tag']),row['resource']),[]).append(row)
    # Every behavioral task is tied to its live reservation and completion.
    for index,node in enumerate(graph['tasks']):
        if 'resource' not in node:continue
        resource = node['resource'];stack = node['stack']
        key = {'host':'gpu_crossbar','gpu_compute':'gpu_compute/card0','transpose':f'layout/s{stack}'}.get(resource,f'{resource}/s{stack}')
        rows = by_resource.get((index,key),[])
        assert len(rows)==1, 'missing/duplicated work reservation'
        actual = rows[0];task = tasks[node['id']]
        assert int(actual['start_cycle'])==int(task['start_cycle']) and int(actual['end_cycle'])==int(task['complete_cycle']), 'work callback horizon'
        assert int(actual['bytes'])==node['bytes'], 'work byte conservation'
    for kernel in manifest['kernels']:
        row = tasks[kernel['task_id']]
        assert int(row['complete_cycle'])-int(row['start_cycle'])==kernel['total_cycles'], 'GPU compute duration'
        assert 0<=kernel['compute_rounding_ps']<config['clock_ps'], 'GPU rounding bound'
    by_source = {}
    for request in records(output/'requests.csv'):
        task = graph['tasks'][int(request['task_index'])];source = task['source'];kind = int(request['type'])
        counts = by_source.setdefault(source,dict(requests=0,read_bytes=0,write_bytes=0,mask_valid_read_bytes=0,mask_valid_write_bytes=0,pim_requests=0))
        counts['requests'] += 1
        if kind<2:
            name = 'write_bytes' if kind else 'read_bytes'
            counts[name] += 32;counts['mask_valid_'+name] += int(request['byte_mask']).bit_count()
        else:counts['pim_requests'] += 1
    allocations = {a['name']:a for a in manifest['allocations']}
    gpu = by_source['gpu']
    assert gpu['mask_valid_read_bytes']==sum(allocations[n]['logical_bytes'] for n in ('input','query_weights','output_weights'))
    assert gpu['mask_valid_write_bytes']==allocations['output']['logical_bytes']
    assert gpu['read_bytes']==sum(allocations[n]['physical_bytes'] for n in ('input','query_weights','output_weights'))
    assert gpu['write_bytes']==allocations['output']['physical_bytes']
    port_bytes = gpu['mask_valid_read_bytes']+gpu['mask_valid_write_bytes']+2*config['stacks']*config['base_die']['head_dim']*2
    for prefix in ('ucie/','gpu_crossbar'):
        assert sum(int(r['bytes']) for r in resources if r['resource'].startswith(prefix))==port_bytes, 'GPU port byte conservation'
    foreground = int(tasks[manifest['foreground_terminal']]['complete_cycle'])
    background = max(int(tasks[t]['complete_cycle']) for t in manifest['background_terminals'])
    assert hardware['complete_cycle']==max(foreground,background), 'terminal time conservation'
    issue = manifest['workload']['issue_cycle'];elapsed = foreground-issue
    report = {'schema_version':1,'acceptance':'passed','foreground_issue_cycle':issue,
              'foreground_complete_cycle':foreground,'foreground_latency_cycles':elapsed,
              'foreground_latency_ps':elapsed*config['clock_ps'],'foreground_latency_us':elapsed*config['clock_ps']/10**6,
              'background_complete_cycle':background,'background_drain_after_foreground_cycles':max(0,background-foreground),
              'all_tasks_complete_cycle':hardware['complete_cycle'],'simulation_end_cycle':hardware['simulation_end_cycle'],
              'maintenance_drain_cycles':hardware['maintenance_drain_cycles'],
              'GPU_compute_busy_cycles':sum(k['total_cycles'] for k in manifest['kernels']),
              'migration_FP16_equivalent_payload_bytes':{kind:sum(m['logical_bytes'] for m in manifest['subgraphs'].values()
                  if m.get('kind')==kind) for kind in ('promotion','demotion')},
              'GPU_port_payload_bytes':port_bytes,'by_source':by_source,'hardware':hardware,
              'byte_accounting':'read/write bytes are physical 32B bursts; masks describe interface-valid bytes, not FP16-equivalent KV payload; PIM internal operand bytes are unmodeled',
              'timing_rule':'terminal callback - issue; concurrent stages are not summed; backend maintenance drain reported separately'}
    (output/'system_verification.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def run_system(output: Path, profile: dict, hardware: dict, trace: dict) -> dict:
    config,contract = configure_system(profile,hardware)
    graph,manifest = compile_workload(profile,config,trace)
    run_case(output,config,graph).check_returncode()
    for name,value in (('system_profile',profile),('trace',trace),('system_contract',contract),('workload_manifest',manifest)):
        (output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    return validate_system(output,config,graph,manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id');args = parser.parse_args()
    if Path(args.run_id).name!=args.run_id or args.run_id in ('.','..'):parser.error('new simple run ID required')
    output = ROOT/'results'/args.run_id;output.mkdir(exist_ok=False)
    profile,hardware,contract = load_system()
    trace = yaml.safe_load((ROOT/'traces/a100a_decode_micro.yaml').read_text())
    cases = {}
    for name in ('baseline','compute_slow','tsv_slow','ucie_slow','crossbar_slow','background_late'):
        p,c,t = copy.deepcopy(profile),copy.deepcopy(hardware),copy.deepcopy(trace)
        if name=='compute_slow':p['gpu']['compute_utilization']=0.0008 # controlled sensitivity, not A100 calibration
        if name=='tsv_slow':c['resources']['tsv_GBps']=1
        if name=='ucie_slow':c['resources']['ucie_GBps']=1
        if name=='crossbar_slow':c['resources']['host_crossbar_GBps']=1
        if name=='background_late':t['background_issue_cycle']=10000
        result = run_system(output/name,p,c,t)
        cases[name] = {k:v for k,v in result.items() if k!='hardware'}
        print(name,result['foreground_latency_cycles'],result['background_complete_cycle'],flush=True)
    base = cases['baseline']
    for name in ('compute_slow','tsv_slow','ucie_slow','crossbar_slow'):
        assert cases[name]['foreground_latency_cycles']>base['foreground_latency_cycles'],name+' insensitive'
    assert cases['background_late']['background_drain_after_foreground_cycles']>0
    assert cases['background_late']['foreground_complete_cycle']<10000, 'background charged to foreground'
    assert contract['capacity_bytes']['total']==60*2**30
    assert contract['attacc_overrides']['OFF_MEM_BW_PER_DEVICE']==2560*10**9
    snapshot(output)
    (output/'system_acceptance.json').write_text(json.dumps({'schema_version':1,'acceptance':'passed',
        'contract':contract,'cases':cases,'limits':'engineering hybrid; no full LLM TPOT or silicon calibration'},indent=2)+'\n')


if __name__=='__main__':main()
