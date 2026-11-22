"""Single-card AttAcc compute parameters coupled to live TokenStack callbacks.

GPU compute is a FLOP service-time model, DRAM is command-timed, and allocation
and DAG construction are functional. No xPU roofline memory time is added.
"""
from __future__ import annotations
import copy
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import sys
import yaml
from hardware_topology import Topology, check
from attention_hardware import attention_graph
from kv_pipeline import migration_graph, align
from throughput import dram_ceiling_GBps

ROOT = Path(__file__).resolve().parent
ATTACC = ROOT.parents[1] / 'attacc_simulator'
sys.path.insert(0, str(ATTACC))
from src.config import make_xpu_config, SCALING_FACTOR
from src.model import Layer
from src.type import GPUType, LayerType, DataType


def configure_system(profile: dict, hardware: dict) -> tuple[dict, dict]:
    check(profile['schema_version'] == 1 and profile['stacks'] == 5, 'requires five-stack system profile')
    gpu = profile['gpu']; memory = profile['memory']
    check(gpu['model'] == 'A100a' and gpu['devices'] == 1 and gpu['precision'] == 'W16A16',
          'only single-card A100a W16A16 is supported; no NVLink')
    check(type(gpu['compute_utilization']) in (int, float) and 0 < gpu['compute_utilization'] <= 1,
          'invalid GPU utilization')
    check(type(gpu['launch_overhead_cycles']) is int and gpu['launch_overhead_cycles'] >= 0, 'invalid launch overhead')
    check(memory['cache_model'] == 'none' and memory['gpu_capacity_bank_fraction'] == 0.5,
          'unsupported cache/Capacity partition')
    chunk = memory['transfer_chunk_bytes']
    check(type(chunk) is int and 32 <= chunk <= 1024 and 1024 % chunk == 0, 'invalid transfer packet size')
    check(profile['timing'] == {'kernel_policy':'load_compute_store', 'clock':'hardware_clock_ps',
          'foreground':'last_foreground_callback_minus_issue', 'background':'report_separate_drain_horizon'},
          'unsupported timing policy')
    effective = copy.deepcopy(hardware); effective['stacks'] = profile['stacks']
    topo = Topology(effective)
    cap = sum(d.size for d in topo.dies if d.kind == 'capacity')
    hot = sum(d.size for d in topo.dies if d.kind == 'compute')
    resources = effective['resources']
    channels = {d.geometry[0] for d in topo.dies}
    check(channels == {16}, 'GPU profile requires 16 channels per stack')
    for key in ('tsv_GBps', 'ucie_GBps', 'host_crossbar_GBps'):
        check(math.isfinite(resources[key]) and resources[key] > 0, 'invalid bandwidth')
    dram = dram_ceiling_GBps(effective)
    host_limit = min(5 * min(dram, resources['tsv_GBps'], resources['ucie_GBps']), resources['host_crossbar_GBps'])
    inherited = make_xpu_config(GPUType.A100a, num_gpu=1, mem_cap=topo.total_bytes, mem_bw=host_limit * 10**9)['GPU']
    effective['gpu_system'] = {'model':'A100a', 'devices':1, 'precision':'W16A16',
        'peak_flops_per_second':inherited['FLOPS_PER_DEVICE'], 'compute_utilization':gpu['compute_utilization'],
        'launch_overhead_cycles':gpu['launch_overhead_cycles']}
    contract = {'schema_version':1, 'profile':profile['profile'], 'gpu':effective['gpu_system'],
        'SMs':inherited['NUM_CORE'], 'attacc_default_compute_utilization':SCALING_FACTOR['MAX_COMPUTE_UTIL'],
        'effective_flops_per_second':inherited['FLOPS_PER_DEVICE'] * gpu['compute_utilization'],
        'capacity_bytes':{'total':topo.total_bytes, 'capacity_layers':cap, 'compute_layers':hot,
                          'gpu_arena':cap//2, 'cold_kv_arena':cap//2},
        'bandwidth_GBps':{'dram_interface_per_stack':dram, 'tsv_per_stack':resources['tsv_GBps'],
                         'ucie_per_stack':resources['ucie_GBps'], 'host_crossbar_total':resources['host_crossbar_GBps'],
                         'host_memory_path_upper_bound':host_limit},
        'attacc_overrides':{'NUM_DEVICE':1, 'MEM_CAPACITY_PER_DEVICE':inherited['MEM_CAPACITY_PER_DEVICE'],
                            'OFF_MEM_BW_PER_DEVICE':inherited['OFF_MEM_BW_PER_DEVICE']},
        'unmodeled_cache_provenance':{'L1_bytes_per_SM':inherited['L1_CAP_PER_CORE'], 'L2_bytes':inherited['L2_CAP_PER_DEVICE']},
        'clock_ps':effective['clock_ps'], 'bandwidth_units':'decimal GB/s = 10^9 B/s; capacities reported in bytes/GiB',
        'compute_policy':'ceil(FLOPs/(peak*utilization)/tCK) + explicit launch overhead; one shared GPU lane',
        'memory_policy':'load -> compute -> store; live DRAM/TSV/UCIe/crossbar callbacks; no analytical memory term',
        'attacc_source_sha256':{name:hashlib.sha256((ATTACC/name).read_bytes()).hexdigest()
                                for name in ('src/config.py','src/model.py','src/type.py')}}
    return effective, contract


def load_system(path: Path = ROOT/'config/tokenstack_a100a.yaml') -> tuple[dict, dict, dict]:
    profile = yaml.safe_load(path.read_text())
    hardware = yaml.safe_load((path.parent/profile['hardware_profile']).read_text())
    effective, contract = configure_system(profile, hardware)
    return profile, effective, contract


def gpu_kernel(config: dict, name: str, m: int, n: int, k: int, *, depends_on: list[str], issue: int = 0) -> tuple[dict, dict]:
    check(all(type(v) is int and v > 0 for v in (m,n,k)), 'invalid GEMM dimensions')
    gpu = config['gpu_system']
    layer = Layer('decode', name, LayerType.FC, True, DataType.W16A16, m, n, k, 1)
    flops = layer.get_flops()
    duration = Fraction(flops * 10**12, gpu['peak_flops_per_second'] * config['clock_ps']) / Fraction(str(gpu['compute_utilization']))
    compute_cycles = math.ceil(duration)
    cycles = compute_cycles + gpu['launch_overhead_cycles']
    check(0 < cycles < 2**63, 'GPU duration overflow')
    return {'id':name, 'source':'gpu', 'depends_on':depends_on, 'issue_cycle':issue,
            'resource':'gpu_compute', 'stack':0, 'bytes':0, 'cycles':cycles}, {
            'task_id':name, 'shape_m_n_k':[m,n,k], 'FLOPs':flops, 'attacc_tensor_bytes':list(layer.get_size()),
            'compute_cycles':compute_cycles, 'launch_cycles':gpu['launch_overhead_cycles'],
            'total_cycles':cycles, 'compute_rounding_ps':float((compute_cycles-duration)*config['clock_ps'])}


class CapacityAllocator:
    """Stripe packets across stack/channel/PC/die/BG/bank, then row/column.

    Upper half of every Capacity bank is a disjoint GPU arena. Lower half is
    reserved for cold KV; all migration requests are checked against this split.
    Padding reserves a full packet but generates only the required 32B bursts.
    """
    def __init__(self, config: dict, chunk: int):
        self.topo = Topology(config); self.chunk = chunk; self.cursor = 0; self.allocations = []
        self.dies = [[d for d in self.topo.dies if d.stack == s and d.kind == 'capacity'] for s in range(config['stacks'])]
        check(all(len(ds)==4 for ds in self.dies), 'allocator requires four Capacity dies per stack')
        check({d.geometry for ds in self.dies for d in ds} == {(16,2,1,4,4,4096,32)}, 'unsupported GPU arena geometry')
        self.limit = sum(d.size//2 for ds in self.dies for d in ds)

    def allocate(self, name: str, size: int) -> list[dict]:
        check(type(size) is int and size > 0, 'invalid tensor size')
        check(name not in {a['name'] for a in self.allocations}, 'duplicate tensor allocation')
        count = (size+self.chunk-1)//self.chunk
        check((self.cursor+count)*self.chunk <= self.limit, 'GPU arena capacity overflow')
        chunks = []
        for part in range(count):
            index = self.cursor+part; lane = index % 640; slot = index//640
            stack = lane%5; channel = (lane//5)%16; pc = (lane//80)%2; d = lane//160
            die = self.dies[stack][d]; offset = die.bank_bytes//2 + (slot//16)*self.chunk
            row, col = divmod(offset//32, 32)
            address = die.encode((channel,pc,0,slot%4,(slot//4)%4,row,col))
            valid = min(self.chunk,size-part*self.chunk)
            chunks.append({'address':address, 'bytes':align(valid), 'valid_bytes':valid, 'stack':stack})
        self.cursor += count
        self.allocations.append({'name':name, 'logical_bytes':size, 'physical_bytes':sum(p['bytes'] for p in chunks),
                                 'reserved_bytes':count*self.chunk, 'chunks':chunks})
        return chunks


def port_transfer(prefix: str, stack: int, size: int, write: bool, dependencies: list[str]) -> tuple[list[dict],str]:
    tasks = []; previous = dependencies
    for resource in (('host','ucie') if write else ('ucie','host')):
        name = prefix+'_'+resource
        tasks.append({'id':name, 'source':'gpu', 'depends_on':previous, 'resource':resource,
                      'stack':stack, 'bytes':size, 'cycles':0})
        previous = [name]
    return tasks, previous[0]


def tensor_io(prefix: str, chunks: list[dict], write: bool, dependencies: list[str]) -> tuple[list[dict],str]:
    tasks = []; terminals = []
    for i, chunk in enumerate(chunks):
        name = f'{prefix}_{i}'; mem = name+'_dram'
        ports, end = port_transfer(name,chunk['stack'],chunk['valid_bytes'],write,dependencies if write else [mem])
        requests = []
        for offset in range(0,chunk['bytes'],32):
            valid = min(32,chunk['valid_bytes']-offset)
            requests.append({'type':int(write), 'address':chunk['address']+offset, 'byte_mask':(1<<valid)-1})
        tasks.extend(ports)
        tasks.append({'id':mem, 'source':'gpu', 'depends_on':[end] if write else dependencies, 'requests':requests})
        terminals.append(mem if write else end)
    done = prefix+'_done'; tasks.append({'id':done, 'source':'gpu', 'depends_on':terminals})
    return tasks, done


def compile_workload(profile: dict, config: dict, trace: dict) -> tuple[dict, dict]:
    check(trace['schema_version']==1 and trace['workload']=='query_projection_pim_attention_output_projection', 'unsupported workload')
    check(config['stacks']==5 and config['gpu_system']['devices']==1, 'system configuration mismatch')
    for key in ('issue_cycle','background_issue_cycle'):
        check(type(trace[key]) is int and trace[key]>=0, 'invalid issue cycle')
    for key in ('hidden_dim','tokens_per_head','background_tokens_per_head'):
        check(type(trace[key]) is int and trace[key]>0, 'invalid workload size')
    hidden = trace['hidden_dim']; dim = config['base_die']['head_dim']; heads = config['stacks']; width = heads*dim
    allocator = CapacityAllocator(config,profile['memory']['transfer_chunk_bytes'])
    x = allocator.allocate('input',hidden*2)
    wq = allocator.allocate('query_weights',hidden*width*2)
    wo = allocator.allocate('output_weights',width*hidden*2)
    y = allocator.allocate('output',hidden*2)
    tasks = [{'id':'decode_begin', 'source':'gpu', 'issue_cycle':trace['issue_cycle']}]
    kernels = []; manifests = {}; loaded = []
    for name, chunks in (('input_load',x),('query_weights_load',wq)):
        nodes,end = tensor_io(name,chunks,False,['decode_begin']); tasks.extend(nodes);loaded.append(end)
    kernel, meta = gpu_kernel(config,'query_projection',1,width,hidden,depends_on=loaded)
    tasks.append(kernel);kernels.append(meta);outputs = [];background = []
    for stack in range(heads):
        promoted, migration = migration_graph(config,tokens=trace['tokens_per_head'],stack=stack,block=0,
                                               prefix=f'promote_s{stack}',depends_on=['decode_begin'])
        tasks.extend(promoted['tasks']); manifests[f'promote_s{stack}'] = migration
        ports,query = port_transfer(f'query_s{stack}',stack,dim*2,True,['query_projection'])
        tasks.extend(ports)
        attention, manifest = attention_graph(config,trace['tokens_per_head'],dim,stack=stack,
            prefix=f'attention_s{stack}',depends_on=[query,migration['done_task']])
        tasks.extend(attention['tasks']); manifests[f'attention_s{stack}'] = manifest
        ports,result = port_transfer(f'output_s{stack}',stack,dim*2,False,[manifest['done_task']])
        tasks.extend(ports);outputs.append(result)
        demoted, migration = migration_graph(config,tokens=trace['background_tokens_per_head'],stack=stack,block=1,
            kind='demotion',prefix=f'demote_s{stack}',issue=trace['background_issue_cycle'])
        tasks.extend(demoted['tasks']);background.append(migration['done_task']);manifests[f'demote_s{stack}'] = migration
    nodes,weights = tensor_io('output_weights_load',wo,False,outputs);tasks.extend(nodes)
    kernel, meta = gpu_kernel(config,'output_projection',1,hidden,width,depends_on=[weights])
    tasks.append(kernel);kernels.append(meta)
    nodes,done = tensor_io('output_store',y,True,['output_projection']);tasks.extend(nodes)
    tasks.append({'id':'decode_done','source':'gpu','depends_on':[done]})
    # Verify disjoint cold/GPU arenas on the actual generated physical requests.
    for task in tasks:
        for req in task.get('requests',[]):
            die = allocator.topo.find(req['address'],32,req['type']>=4)
            if die.kind=='capacity':
                offset = (req['address']-die.base) % die.bank_bytes
                check((offset>=die.bank_bytes//2)==(task['source']=='gpu'), 'cold KV/GPU arena overlap')
    report = {'schema_version':1,'workload':trace,'kernels':kernels,'allocations':allocator.allocations,
              'gpu_reserved_bytes':allocator.cursor*allocator.chunk,'subgraphs':manifests,
              'foreground_terminal':'decode_done','background_terminals':background,
              'initial_state':'weights, input and cold block 0 / hot block 1 resident at issue; no initialization cost',
              'resident_intermediates':'query and attention output stay on GPU between explicit host-port transfers',
              'limitations':'synthetic five-head one-query timing workload; no full decoder, KV append, numerical GPU or cache execution'}
    return {'schema_version':1,'tasks':tasks}, report
