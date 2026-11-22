"""Sparse physical topology and static KV layouts; no hardware timing estimates."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

AXES = ("channel", "pseudochannel", "rank", "bankgroup", "bank", "row", "column")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Die:
    stack: int
    die_id: int
    kind: str
    base: int
    geometry: tuple[int, ...]
    burst: int

    @property
    def size(self) -> int:
        return math.prod(self.geometry) * self.burst

    @property
    def banks(self) -> int:
        return math.prod(self.geometry[:5])

    @property
    def bank_bytes(self) -> int:
        return math.prod(self.geometry[5:]) * self.burst

    def encode(self, coordinates: tuple[int, ...], byte: int = 0) -> int:
        check(len(coordinates) == 7 and 0 <= byte < self.burst, "invalid coordinates/byte")
        offset = 0
        for value, count in zip(coordinates, self.geometry):
            check(type(value) is int and 0 <= value < count, "coordinate outside geometry")
            offset = offset * count + value
        return self.base + offset * self.burst + byte

    def decode(self, address: int) -> tuple[tuple[int, ...], int]:
        offset = address - self.base
        check(0 <= offset < self.size, "address outside die")
        value, byte = divmod(offset, self.burst)
        coordinates = []
        for count in reversed(self.geometry):
            value, part = divmod(value, count)
            coordinates.append(part)
        check(value == 0, "address alias")
        return tuple(reversed(coordinates)), byte

    def bank_address(self, bank: int, byte_offset: int) -> int:
        check(0 <= bank < self.banks and 0 <= byte_offset < self.bank_bytes, "bank range overflow")
        coordinates = []
        for count in reversed(self.geometry[:5]):
            bank, part = divmod(bank, count)
            coordinates.append(part)
        row_column, byte = divmod(byte_offset, self.burst)
        row, column = divmod(row_column, self.geometry[6])
        return self.encode((*reversed(coordinates), row, column), byte)


class Topology:
    def __init__(self, config: dict[str, Any]):
        check(config["schema_version"] == 1 and type(config["stacks"]) is int and 1 <= config["stacks"] <= 5,
              "unsupported topology schema/stack count")
        check(config["paper_spec_frozen"] is False, "this engineering profile cannot claim paper freeze")
        self.config = config
        self.dies: list[Die] = []
        burst = config["dram"]["dq_bits"] * config["dram"]["prefetch"] // 8
        check(burst == 32 and config["clock_ps"] == 769, "unsupported AttAcc transfer/clock")
        ids, order = set(), []
        for entry in config["dies"]:
            check(entry["die_id"] not in ids and entry["kind"] in ("capacity", "compute"), "duplicate/invalid die")
            ids.add(entry["die_id"])
            order.append(entry["vertical_order"])
            geometry = entry["geometry"]
            check(len(geometry) == 7 and all(type(n) is int and n > 0 and n & (n - 1) == 0 for n in geometry),
                  "bit-slice geometry requires power-of-two axes")
            check(math.prod(geometry) * burst == entry["bytes"], "die capacity disagrees with geometry")
        check(order == list(range(len(order))), "invalid vertical order")
        check([x["kind"] for x in config["dies"]] == sorted([x["kind"] for x in config["dies"]]),
              "capacity dies must be below compute dies")
        base = 0
        for stack in range(config["stacks"]):
            for entry in config["dies"]:
                die = Die(stack, entry["die_id"], entry["kind"], base, tuple(entry["geometry"]), burst)
                self.dies.append(die)
                base += die.size
        check(base < 2**63, "global address overflow")
        self.total_bytes = base
        for kind in ("capacity", "compute"):
            check(any(d.kind == kind for d in self.dies), "missing layer kind")

    def find(self, address: int, length: int = 1, pim: bool = False) -> Die:
        check(type(address) is int and type(length) is int and length > 0, "invalid address/length")
        for die in self.dies:
            if die.base <= address and address + length <= die.base + die.size:
                check(not pim or die.kind == "compute", "PIM forbidden on Capacity")
                return die
        raise ValueError("address outside physical die")

    def bank_location(self, stack: int, kind: str, index: int) -> tuple[Die, int]:
        dies = [d for d in self.dies if d.stack == stack and d.kind == kind]
        check(bool(dies), "unknown stack/kind")
        total = sum(d.banks for d in dies)
        check(0 <= index < total, "bank index outside stack")
        # Die is the fastest axis: cold successive pages expose all Capacity dies.
        # Equal per-kind geometry required by this profile.
        check(len({d.geometry for d in dies}) == 1, "heterogeneous per-kind enumeration unsupported")
        return dies[index % len(dies)], index // len(dies)

    def cold_page(self, stack: int, page: int, bytes_per_page: int) -> int:
        check(page >= 0 and bytes_per_page > 0 and bytes_per_page % 32 == 0, "invalid cold page")
        total = sum(d.banks for d in self.dies if d.stack == stack and d.kind == "capacity")
        check(total > 0, "unknown stack")
        slot, bank = divmod(page, total)
        die, local_bank = self.bank_location(stack, "capacity", bank)
        check((slot + 1) * bytes_per_page <= die.bank_bytes, "cold bank page overflow")
        return die.bank_address(local_bank, slot * bytes_per_page)

    def kv_address(self, stack: int, tensor: str, token: int, feature: int,
                   tokens: int, dim: int, block: int = 0) -> int:
        check(tensor in ("K", "V") and tokens > 0 and dim > 0 and block >= 0, "invalid KV shape")
        check(0 <= token < tokens and 0 <= feature < dim, "KV index outside shape")
        banks = sum(d.banks for d in self.dies if d.stack == stack and d.kind == "compute")
        check(banks > 0, "unknown stack")
        k_stride = math.ceil(tokens / banks) * dim * 2
        v_stride = math.ceil(dim / banks) * tokens * 2
        # Align each tensor/block region to bursts without sharing padding.
        k_stride = math.ceil(k_stride / 32) * 32
        v_stride = math.ceil(v_stride / 32) * 32
        stride = self.config["base_die"]["kv_block_bank_stride_bytes"]
        check(stride>0 and stride%32==0 and k_stride+v_stride<=stride,"KV shape exceeds block reservation")
        if tensor == "K":
            bank, slot = token % banks, token // banks * dim + feature
            offset = block * stride + slot * 2
        else:
            bank, slot = feature % banks, feature // banks * tokens + token
            offset = block * stride + k_stride + slot * 2
        die, local_bank = self.bank_location(stack, "compute", bank)
        check((block + 1) * stride <= die.bank_bytes, "KV block exceeds bank")
        return die.bank_address(local_bank, offset)

    def ramulator_layers(self, log_dir: str) -> list[dict[str, Any]]:
        layers = []
        for die in self.dies:
            org = dict(zip(AXES, die.geometry))
            org.update(dq=32, density=die.size * 8 // die.geometry[0] // 2**20)
            timing = {"preset": self.config["dram"]["timing"]}
            timing.update({key: self.config["dram"][key] for key in ("nRFC", "nREFISB", "nREFI")})
            layers.append({"stack": die.stack, "die_id": die.die_id, "kind": die.kind, "base": die.base,
                           "bytes": die.size, "backend": {"impl": "PIMDRAM", "clock_ratio": 1,
                           "DRAM": {"impl": "HBM3-PIM", "org": org, "timing": timing,"tokenstack_physical_dies":True},
                           "AddrMapper": {"impl": "HBM3-PIM"},
                           "Controller": {"impl": "HBM3-PIM", "tokenstack_closed_loop": True,
                                          "Scheduler": {"impl": "PIM"}, "RefreshManager": {"impl": "AllBankHBM3"},
                                          "plugins": [{"ControllerPlugin": {"impl": "HBM3TraceRecorder",
                                              "path": f"{log_dir}/s{die.stack}-d{die.die_id}.cmd"}}]}}})
        return layers
