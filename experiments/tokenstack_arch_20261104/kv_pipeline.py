"""Versioned page-DMA DAGs and a separate functional K8V4/layout reference.

The codec is an explicit symmetric groupwise engineering choice, not the
paper's undisclosed numerical implementation. No tensor compute is simulated
inside Ramulator; addresses and stage dependencies are compiled here.
"""
from __future__ import annotations
import math
import struct
from typing import Any
from hardware_topology import Topology,check


def align(value: int, burst: int=32) -> int:
    return (value+burst-1)//burst*burst


def half(value: float) -> float:
    check(math.isfinite(value),"non-finite KV value")
    return struct.unpack('<e',struct.pack('<e',value))[0]


def quantize(values: list[float],bits: int,group: int) -> tuple[bytes,list[float]]:
    check(bits in (4,8) and group>0,"unsupported quantization")
    maximum=2**(bits-1)-1;codes=[];scales=[]
    for offset in range(0,len(values),group):
        part=[half(x) for x in values[offset:offset+group]]
        largest=max(abs(x) for x in part)
        scale=half(largest/maximum) if largest else 1.0
        if scale==0:scale=2**-24
        scales.append(scale)
        codes += [max(-maximum,min(maximum,round(x/scale))) for x in part]
    if bits==8:payload=bytes(x&255 for x in codes)
    else:payload=bytes((codes[i]&15)|((codes[i+1]&15)<<4 if i+1<len(codes) else 0) for i in range(0,len(codes),2))
    return payload+b''.join(struct.pack('<e',x) for x in scales),scales


def dequantize(data: bytes,count: int,bits: int,group: int) -> list[float]:
    payload_bytes=(count*bits+7)//8;groups=math.ceil(count/group)
    check(len(data)==payload_bytes+2*groups,"quantized payload/metadata size")
    scales=struct.unpack('<'+'e'*groups,data[payload_bytes:])
    values=[]
    for i in range(count):
        code=data[i] if bits==8 else (data[i//2]>>(4*(i%2)))&15
        if code & (1<<(bits-1)):code-=1<<bits
        values.append(half(code*scales[i//group]))
    return values


def page_sizes(tokens: int,dim: int,group: int) -> dict[str,int]:
    check(tokens>0 and dim>0 and group>0,"invalid page shape")
    elements=tokens*dim;k=elements;v=(elements+1)//2
    metadata=4*math.ceil(elements/group)
    return {"elements":elements,"fp16_payload":elements*4,"k_payload":k,"v_payload":v,
            "metadata":metadata,"cold_payload":k+v,"cold_physical":align(k+v+metadata)}


def hot_addresses(topo: Topology,stack: int,tokens: int,dim: int,start: int,count: int,block: int) -> list[int]:
    addresses={topo.kv_address(stack,tensor,n,f,tokens,dim,block)//32*32
               for tensor in ("K","V") for n in range(start,start+count) for f in range(dim)}
    return sorted(addresses)


def hot_masks(topo: Topology,stack: int,tokens: int,dim: int,start: int,count: int,block: int) -> dict[int,int]:
    masks={}
    for tensor in ("K","V"):
        for n in range(start,start+count):
            for f in range(dim):
                address=topo.kv_address(stack,tensor,n,f,tokens,dim,block);burst=address//32*32
                masks[burst]=masks.get(burst,0)|(3<<(address%32))
    return masks


def migration_graph(config: dict[str,Any], *, tokens: int=20,kind: str="promotion",stack: int=0,
                    block: int=0,prefix: str="move",depends_on: list[str]|None=None,
                    issue: int=0,head_group: int=0,cold_page_base: int|None=None) -> tuple[dict,dict]:
    check(kind in ("promotion","demotion") and tokens>0,"invalid migration")
    topo=Topology(config);base=config["base_die"];dim=base["head_dim"];page=base["page_tokens"]
    check(base["metadata_bytes_per_group"]==2,"reference codec requires FP16 scale")
    tasks=[];pages=[];logical=read_total=write_total=0
    check(math.ceil(tokens/page)<=base["cold_pages_per_block"],"logical block exceeds cold page reservation")
    if cold_page_base is None:cold_page_base=block*base["cold_pages_per_block"]
    max_sizes=page_sizes(page,dim,base["quant_group_elements"])
    for p,start in enumerate(range(0,tokens,page)):
        count=min(page,tokens-start);sizes=page_sizes(count,dim,base["quant_group_elements"])
        name=f"{prefix}_p{p}";acquire=name+"_acquire"
        tasks.append({"id":acquire,"source":kind,"issue_cycle":issue,"depends_on":depends_on or [],
                      "acquire":name,"buffer_group":head_group+stack*base["head_groups"],"buffer_bytes":max_sizes["fp16_payload"],
                      "page_lock":f"s{stack}_h{head_group}_block{block}_page{p}"})
        cold=topo.cold_page(stack,cold_page_base+p,max_sizes["cold_physical"])
        cold_bursts=list(range(cold,cold+sizes["cold_physical"],32))
        hot=hot_addresses(topo,stack,tokens,dim,start,count,block)
        masks=hot_masks(topo,stack,tokens,dim,start,count,block)
        reads,writes=(cold_bursts,hot) if kind=="promotion" else (hot,cold_bursts)
        conversions=[]
        # Burst-level conversions can overlap later source reads. Transpose
        # waits for the complete page; the next page can read into ping-pong SRAM.
        for r,address in enumerate(reads):
            rid=f"{name}_rd{r}";cid=f"{name}_convert{r}";conversions.append(cid)
            tasks.append({"id":rid,"source":kind,"depends_on":[acquire],"requests":[{"type":0,"address":address}]})
            tasks.append({"id":cid,"source":kind,"depends_on":[rid],"resource":"convert","stack":stack,
                          "bytes":math.ceil(max(len(reads),len(writes))*32/len(reads)),"cycles":0})
        transpose=name+"_layout"
        tasks.append({"id":transpose,"source":kind,"depends_on":conversions,"resource":"transpose","stack":stack,
                      "bytes":sizes["fp16_payload"],"cycles":0})
        wid=name+"_write"
        # Ordinary masked writes preserve unrelated page bytes sharing a burst.
        # Ramulator models full burst timing; the functional reference checks
        # masks/layout, rather than inventing zero-cost read-modify-write traffic.
        tasks.append({"id":wid,"source":kind,"depends_on":[transpose],"requests":[
            {"type":1,"address":a,"byte_mask":masks[a] if kind=="promotion" else 0xffffffff} for a in writes]})
        commit=name+"_commit"
        tasks.append({"id":commit,"source":kind,"depends_on":[wid],"release":name})
        pages.append({"page_id":name,"start_token":start,"tokens":count,"sizes":sizes,"cold_address":cold,
                      "source_bursts":len(reads),"target_bursts":len(writes),"commit_task":commit,
                      "hot_valid_bytes":sum(mask.bit_count() for mask in masks.values()),
                      "hot_burst_padding_bytes":len(hot)*32-sizes["fp16_payload"]})
        logical+=sizes["fp16_payload"];read_total+=32*len(reads);write_total+=32*len(writes)
    final=prefix+"_done"
    tasks.append({"id":final,"source":kind,"depends_on":[p["commit_task"] for p in pages]})
    # Reserve complete FP16 pages; partial last-page padding and hot scatter
    # burst amplification are explicit in per-page physical counts.
    return {"schema_version":1,"tasks":tasks},{"schema_version":1,"kind":kind,"tokens":tokens,"dim":dim,
           "logical_bytes":logical,"read_bytes":read_total,"write_bytes":write_total,"pages":pages,"done_task":final,
           "page_buffer_bytes":max_sizes["fp16_payload"],"codec":"symmetric_groupwise_fp16_scale_engineering_v1"}


def functional_roundtrip(config: dict,tokens: int,dim: int,keys: list[float],values: list[float],block: int=0) -> dict:
    """Execute quantize/dequantize/scatter/transpose/gather on sparse bytes."""
    check(len(keys)==len(values)==tokens*dim,"KV element count")
    topo=Topology(config);group=config["base_die"]["quant_group_elements"];page=config["base_die"]["page_tokens"]
    hot={};decoded={"K":[],"V":[]};cold_bytes=0;max_error=0.0
    for start in range(0,tokens,page):
        count=min(page,tokens-start)
        for tensor,source,bits in (("K",keys,8),("V",values,4)):
            payload=source[start*dim:(start+count)*dim];encoded,scales=quantize(payload,bits,group)
            restored=dequantize(encoded,len(payload),bits,group);cold_bytes+=len(encoded)
            for i,value in enumerate(restored):
                n,f=divmod(i,dim);address=topo.kv_address(0,tensor,start+n,f,tokens,dim,block)
                check(address not in hot,"layout alias")
                hot[address]=struct.pack('<e',value)
                max_error=max(max_error,abs(half(payload[i])-value))
            decoded[tensor].extend(restored)
    gathered={tensor:[struct.unpack('<e',hot[topo.kv_address(0,tensor,n,f,tokens,dim,block)])[0]
                       for n in range(tokens) for f in range(dim)] for tensor in ("K","V")}
    check(gathered==decoded,"scatter/transpose inverse gather mismatch")
    return {"stored_elements":len(hot),"cold_bytes_excluding_padding":cold_bytes,"max_quantization_error":max_error,
            "keys":gathered["K"],"values":gathered["V"],"quantization_is_lossy":True}
