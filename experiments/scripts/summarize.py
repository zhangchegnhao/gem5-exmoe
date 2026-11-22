#!/usr/bin/env python3
"""Print selected statistics; preserve raw stats.txt for research analyses."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("stats", type=Path)
args = parser.parse_args()
values = {}
for line in args.stats.read_text().splitlines():
    fields = line.split()
    if len(fields) >= 2 and not fields[0].startswith(("#", "-")):
        values[fields[0]] = fields[1]
keys = [
    "simSeconds", "simTicks", "simInsts", "hostSeconds",
    "system.cpu.numCycles", "system.cpu.cpi", "system.cpu.ipc",
    "system.cpu.dcache.overallMissRate::total",
    "system.l2cache.overallMissRate::total",
]
print(f"Statistics: {args.stats}")
for key in keys:
    if key in values:
        print(f"  {key}: {values[key]}")
