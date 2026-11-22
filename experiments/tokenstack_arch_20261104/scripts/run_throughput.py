"""TSV throughput acceptance; new output directory required."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import yaml
from run_hardware import ROOT,run_case,validate_fabric,snapshot,records
sys.path.insert(0,str(ROOT))
from throughput import provision,read_stream,dram_ceiling_GBps


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id');args=parser.parse_args()
    if Path(args.run_id).name!=args.run_id or args.run_id in ('.','..'):parser.error('new simple run ID required')
    output=ROOT/'results'/args.run_id;output.mkdir(exist_ok=False)
    original=yaml.safe_load((ROOT/'config/tokenstack_hardware.yaml').read_text());cases={}
    for name,ch,dies,bw,stacks in [('single',1,1,896,1),('four',4,1,896,1),('sixteen',16,1,896,1),
            ('limit448',16,1,448,1),('limit112',16,1,112,1),('limit16',16,1,16,1),
            ('four_dies',16,4,112,1),('two_stacks',16,1,112,2)]:
        config=provision(original,bw,stacks);graph=read_stream(config,ch,dies,64//dies)
        path=output/name;run_case(path,config,graph).check_returncode();summary=validate_fabric(path,config,graph)
        cases[name]={'channels':ch,'dies':dies,'stacks':stacks,'tsv_budget_GBps':bw,
                     'dram_ceiling_GBps_per_stack':dram_ceiling_GBps(config,ch),
                     'complete_cycle':summary['complete_cycle'],'requests':summary['requests'],
                     'read_bytes':summary['read_bytes'],
                     'stack_bandwidth_GBps':{str(s):summary['resource_occupancy'][f'tsv/s{s}']['effective_GBps'] for s in range(stacks)}}
        for rate in cases[name]['stack_bandwidth_GBps'].values():
            assert rate<=min(bw,dram_ceiling_GBps(config,ch))+1e-8,'exceeds theoretical interface limit'
        print(name,cases[name]['stack_bandwidth_GBps'],flush=True)
    rate=lambda name:cases[name]['stack_bandwidth_GBps']['0']
    assert rate('four')>2.5*rate('single')
    assert rate('sixteen')>.65*cases['sixteen']['dram_ceiling_GBps_per_stack']
    assert rate('limit448')>.7*448 and rate('sixteen')>rate('limit448')*1.2
    assert rate('limit112')>.75*112 and rate('sixteen')>rate('limit112')*3
    assert rate('four_dies')>.7*112
    assert all(x>.7*112 for x in cases['two_stacks']['stack_bandwidth_GBps'].values())
    snapshot(output)
    (output/'throughput.json').write_text(json.dumps({'schema_version':1,'acceptance':'passed','cases':cases,
        'model':'cycle-level fluid TSV byte rate with exclusive per-PC bursts',
        'bandwidth_denominator':'simulation_end_cycle * clock_ps from common issue=0, including refresh/drain',
        'queue_profile':{'queue_depth_per_source':256,'dispatch_per_source_cycle':16},
        'limits':'synthetic row-hit loads, large queues; not measured hardware bandwidth or paper calibration'},indent=2)+'\n')


if __name__=='__main__':main()
