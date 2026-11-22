"""Validated Ramulator2 retirement adapter for conservative offline replay.

Only one active Channel is supported. Elapsed intervals include prerequisites,
queueing and DRAM timing gaps: they are replayed once, never summed as per-request
arrive-to-retire latencies. Final command issue is not arithmetic completion.
"""
from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

from tokenstack_model import AttAccPIMRequest, LayerKind, TokenStack


TYPE_TO_COMMAND = {
    4: "PIM_MAC_AB", 5: "PIM_MAC_SB", 6: "PIM_MAC_PB", 7: "PIM_WR_GB",
    8: "PIM_MV_SB", 9: "PIM_MV_GB", 10: "PIM_SFM", 11: "PIM_SET_MODEL",
    12: "PIM_SET_HEAD", 13: "PIM_BARRIER",
}


def load_retirements(path: Path, input_trace: Path) -> list[dict]:
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    expected = [line.split() for line in input_trace.read_text().splitlines() if line.strip()]
    if len(rows) != len(expected) or not rows:
        raise ValueError("retired request count does not match nonempty input trace")
    ids = set()
    previous = 0
    for row in rows:
        for key in ("schema_version", "source_id", "type_id", "address", "channel",
                    "arrive_cycle", "retire_cycle", "tck_ps"):
            row[key] = int(row[key])
        if row["schema_version"] != 1:
            raise ValueError("unsupported retirement trace schema")
        index = row["source_id"]
        if index in ids or not 0 <= index < len(expected):
            raise ValueError("duplicate or invalid source_id")
        ids.add(index)
        if TYPE_TO_COMMAND.get(row["type_id"]) != expected[index][0]:
            raise ValueError("request type changed between input and retirement")
        if row["address"] != int(expected[index][1], 0):
            raise ValueError("request address changed between input and retirement")
        event = "barrier_removed" if row["type_id"] == 13 else "final_command_issued"
        if row["event"] != event:
            raise ValueError("incorrect retirement event semantics")
        if not 0 <= row["arrive_cycle"] <= row["retire_cycle"] or row["retire_cycle"] < previous:
            raise ValueError("invalid retirement chronology")
        previous = row["retire_cycle"]
    if len({row["tck_ps"] for row in rows}) != 1 or rows[0]["tck_ps"] <= 0:
        raise ValueError("clock period must be positive and consistent")
    if len({row["channel"] for row in rows}) != 1:
        raise ValueError("multi-channel retirement replay requires an explicit dependency model")
    return rows


def replay_requests(rows: list[dict], stack: TokenStack) -> list[AttAccPIMRequest]:
    requests = []
    previous = 0
    for row in rows:
        if not 0 <= row["channel"] < stack.channels_per_stack:
            raise ValueError("Ramulator channel outside TokenStack topology")
        physical = stack.map_address(
            row["address"] - row["address"] % stack.cache_line_bytes, LayerKind.COMPUTE)
        # Use Ramulator's decoded Channel, not the different synthetic line mapper.
        physical = replace(physical, channel=row["channel"])
        requests.append(AttAccPIMRequest(
            request_id=f"pim-{row['source_id']}", command=TYPE_TO_COMMAND[row["type_id"]],
            address=row["address"], physical=physical,
            bytes=0,  # PIM instructions do not imply 32-byte DRAM reads/writes.
            issue_cycle=previous, duration_cycles=row["retire_cycle"] - previous,
        ))
        previous = row["retire_cycle"]
    return requests
