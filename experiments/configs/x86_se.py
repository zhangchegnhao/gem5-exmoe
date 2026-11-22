"""Single-core x86 SE baseline for gem5 v24.1.0.3 (classic caches)."""

import argparse
from pathlib import Path

import m5
from m5.objects import (
    AddrRange, Cache, DDR3_1600_8x8, L2XBar, MemCtrl, Process, Root,
    SEWorkload, SrcClockDomain, System, SystemXBar, VoltageDomain,
    X86AtomicSimpleCPU, X86O3CPU, X86TimingSimpleCPU,
)


class L1(Cache):
    assoc = 2
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 4
    tgts_per_mshr = 20


class L2(Cache):
    assoc = 8
    tag_latency = 20
    data_latency = 20
    response_latency = 20
    mshrs = 20
    tgts_per_mshr = 12


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cpu", choices=("timing", "atomic", "o3"), default="timing")
parser.add_argument("--clock", default="1GHz")
parser.add_argument("--l1i", default="32KiB")
parser.add_argument("--l1d", default="32KiB")
parser.add_argument("--l2", default="256KiB")
parser.add_argument("binary", type=Path)
parser.add_argument("program_args", nargs=argparse.REMAINDER)
args = parser.parse_args()
binary = args.binary.resolve()
if not binary.is_file():
    parser.error(f"Binary does not exist: {binary}")

system = System()
system.clk_domain = SrcClockDomain(
    clock=args.clock, voltage_domain=VoltageDomain()
)
system.mem_mode = "atomic" if args.cpu == "atomic" else "timing"
# Match the capacity of one DDR3_1600_8x8 device/rank configuration.
system.mem_ranges = [AddrRange("8GiB")]
system.cache_line_size = 64
cpu_types = {
    "timing": X86TimingSimpleCPU,
    "atomic": X86AtomicSimpleCPU,
    "o3": X86O3CPU,
}
system.cpu = cpu_types[args.cpu]()
system.cpu.icache = L1(size=args.l1i, is_read_only=True, writeback_clean=True)
system.cpu.dcache = L1(size=args.l1d)
system.cpu.icache_port = system.cpu.icache.cpu_side
system.cpu.dcache_port = system.cpu.dcache.cpu_side

system.l2bus = L2XBar()
system.cpu.icache.mem_side = system.l2bus.cpu_side_ports
system.cpu.dcache.mem_side = system.l2bus.cpu_side_ports
system.l2cache = L2(size=args.l2)
system.l2cache.cpu_side = system.l2bus.mem_side_ports
system.membus = SystemXBar()
system.l2cache.mem_side = system.membus.cpu_side_ports
system.system_port = system.membus.cpu_side_ports

system.cpu.createInterruptController()
system.cpu.interrupts[0].pio = system.membus.mem_side_ports
system.cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
system.cpu.interrupts[0].int_responder = system.membus.mem_side_ports

system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = DDR3_1600_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports
system.workload = SEWorkload.init_compatible(str(binary))
process = Process()
process.cmd = [str(binary)] + args.program_args
system.cpu.workload = process
system.cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()
event = m5.simulate()
print(f"Simulation exited at tick {m5.curTick()}: {event.getCause()}")
if event.getCause() != "exiting with last active thread context":
    raise RuntimeError(f"Unexpected simulation exit: {event.getCause()}")
if event.getCode() != 0:
    raise RuntimeError(f"Workload returned error code {event.getCode()}")
