"""Minimal TokenStack topology and migration behavior model.

Precision: behavior-level with cycle approximation for a shared Base Die DMA
engine. It models layer placement, deterministic address mapping, a bounded
page buffer, and serialized TSV transfers. It does not replace Ramulator2's
DRAM command timing and does not claim RTL or HBM4 implementation fidelity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml


class LayerKind(str, Enum):
    CAPACITY = "capacity"
    COMPUTE = "compute"


@dataclass(frozen=True)
class Layer:
    stack_id: int
    layer_id: int
    kind: LayerKind
    capacity_bytes: int


@dataclass(frozen=True)
class PhysicalAddress:
    stack_id: int
    layer_id: int
    layer_kind: LayerKind
    channel: int
    pseudo_channel: int
    bank_group: int
    bank: int
    row: int
    column: int


@dataclass(frozen=True)
class MigrationRequest:
    request_id: str
    source: PhysicalAddress
    target: PhysicalAddress
    bytes_fp16: int
    priority: int
    issue_cycle: int
    quantize: bool


@dataclass(frozen=True)
class MigrationResult:
    request_id: str
    start_cycle: int
    complete_cycle: int
    source_bytes: int
    transferred_bytes: int
    queue_cycles: int
    read_cycles: int
    write_cycles: int
    quantization_cycles: int
    buffer_peak_bytes: int


@dataclass(frozen=True)
class AttAccPIMRequest:
    request_id: str
    command: str
    address: int
    physical: PhysicalAddress
    bytes: int
    issue_cycle: int
    duration_cycles: int
    priority: int = 0


@dataclass(frozen=True)
class MigrationSpec:
    request_id: str
    source: PhysicalAddress
    target: PhysicalAddress
    bytes_fp16: int
    issue_cycle: int
    priority: int


@dataclass(frozen=True)
class ArbitrationResult:
    request_id: str
    request_type: str
    start_cycle: int
    complete_cycle: int
    queue_cycles: int
    channel_ids: Tuple[int, ...]
    base_die_cycles: int
    tsv_cycles: int
    logical_read_bytes: int
    logical_write_bytes: int
    physical_read_bytes: int
    physical_write_bytes: int
    command: str = ""


@dataclass
class ArbitrationStats:
    pim_requests: int = 0
    migration_requests: int = 0
    pim_command_bytes: int = 0
    migration_logical_read_bytes: int = 0
    migration_logical_write_bytes: int = 0
    migration_physical_read_bytes: int = 0
    migration_physical_write_bytes: int = 0
    queue_cycles: int = 0
    base_die_busy_cycles: int = 0
    tsv_busy_cycles: int = 0
    last_complete_cycle: int = 0

    def as_dict(self) -> Dict[str, int]:
        return self.__dict__.copy()


@dataclass
class Stats:
    promotions: int = 0
    demotions: int = 0
    promotion_bytes_fp16: int = 0
    demotion_bytes_fp16: int = 0
    transferred_bytes: int = 0
    queue_cycles: int = 0
    transfer_cycles: int = 0
    quantization_cycles: int = 0
    max_buffer_bytes: int = 0
    last_complete_cycle: int = 0

    def as_dict(self) -> Dict[str, int]:
        return self.__dict__.copy()


@dataclass
class TokenStack:
    num_stacks: int
    channels_per_stack: int
    pseudo_channels_per_channel: int
    bank_groups_per_pseudo_channel: int
    banks_per_bank_group: int
    capacity_layers: int
    compute_layers: int
    capacity_layer_bytes: int
    compute_layer_bytes: int
    cache_line_bytes: int
    row_bytes: int
    clock_hz: int
    tsv_dma_bandwidth_GBps: float
    buffer_bytes: int
    dma_queue_depth: int
    quantization_mode: str
    quantization_ratio: float
    layers: Tuple[Layer, ...] = field(init=False)
    stats: Stats = field(default_factory=Stats, init=False)
    _dma_available_cycle: int = field(default=0, init=False)
    _buffer_in_use: int = field(default=0, init=False)

    @classmethod
    def from_yaml(cls, path: Path) -> "TokenStack":
        raw = yaml.safe_load(path.read_text())
        topo = raw["topology"]
        base = raw["base_die"]
        return cls(
            num_stacks=topo["num_stacks"],
            channels_per_stack=topo["channels_per_stack"],
            pseudo_channels_per_channel=topo["pseudo_channels_per_channel"],
            bank_groups_per_pseudo_channel=topo["bank_groups_per_pseudo_channel"],
            banks_per_bank_group=topo["banks_per_bank_group"],
            capacity_layers=topo["capacity_layers"],
            compute_layers=topo["compute_layers"],
            capacity_layer_bytes=topo["capacity_layer_bytes"],
            compute_layer_bytes=topo["compute_layer_bytes"],
            cache_line_bytes=topo["cache_line_bytes"],
            row_bytes=topo["row_bytes"],
            clock_hz=base["clock_hz"],
            tsv_dma_bandwidth_GBps=base["tsv_dma_bandwidth_GBps"],
            buffer_bytes=base["buffer_bytes"],
            dma_queue_depth=base["dma_queue_depth"],
            quantization_mode=base["quantization_mode"],
            quantization_ratio=base["quantization_ratio"],
        )

    def __post_init__(self) -> None:
        if self.num_stacks < 1 or self.channels_per_stack < 1:
            raise ValueError("num_stacks and channels_per_stack must be positive")
        if self.capacity_layers < 1 or self.compute_layers < 1:
            raise ValueError("at least one capacity and one compute layer are required")
        if self.cache_line_bytes <= 0 or self.row_bytes <= 0:
            raise ValueError("address granularity must be positive")
        if self.tsv_dma_bandwidth_GBps <= 0 or self.clock_hz <= 0:
            raise ValueError("DMA bandwidth and clock must be positive")
        if self.buffer_bytes <= 0 or self.dma_queue_depth <= 0:
            raise ValueError("Base Die buffer and DMA queue must be positive")
        if self.quantization_ratio < 1:
            raise ValueError("quantization ratio must be >= 1")

        layers: List[Layer] = []
        for stack_id in range(self.num_stacks):
            for layer_id in range(self.capacity_layers):
                layers.append(Layer(stack_id, layer_id, LayerKind.CAPACITY, self.capacity_layer_bytes))
            for index in range(self.compute_layers):
                layer_id = self.capacity_layers + index
                layers.append(Layer(stack_id, layer_id, LayerKind.COMPUTE, self.compute_layer_bytes))
        object.__setattr__(self, "layers", tuple(layers))

    @property
    def bytes_per_cycle(self) -> float:
        return self.tsv_dma_bandwidth_GBps * 1_000_000_000 / self.clock_hz

    def layer(self, stack_id: int, layer_id: int) -> Layer:
        for candidate in self.layers:
            if candidate.stack_id == stack_id and candidate.layer_id == layer_id:
                return candidate
        raise ValueError(f"unknown layer stack={stack_id} layer={layer_id}")

    def map_address(self, logical_address: int, kind: LayerKind, stack_id: int = 0) -> PhysicalAddress:
        """Map a logical byte address into a layer and HBM hierarchy.

        Layer selection is explicit: capacity and compute use separate domains;
        lower address bits then distribute lines through channel hierarchy.
        """
        if logical_address < 0 or logical_address % self.cache_line_bytes:
            raise ValueError("logical address must be a non-negative cache-line address")
        if not 0 <= stack_id < self.num_stacks:
            raise ValueError("invalid stack id")
        domain_layers = self.capacity_layers if kind is LayerKind.CAPACITY else self.compute_layers
        domain_bytes = self.capacity_layer_bytes if kind is LayerKind.CAPACITY else self.compute_layer_bytes
        layer_slot = (logical_address // domain_bytes) % domain_layers
        layer_id = layer_slot if kind is LayerKind.CAPACITY else self.capacity_layers + layer_slot
        line = logical_address // self.cache_line_bytes
        channel = line % self.channels_per_stack
        line //= self.channels_per_stack
        pseudo = line % self.pseudo_channels_per_channel
        line //= self.pseudo_channels_per_channel
        bank_group = line % self.bank_groups_per_pseudo_channel
        line //= self.bank_groups_per_pseudo_channel
        bank = line % self.banks_per_bank_group
        row = (logical_address // self.row_bytes) % (1 << 14)
        column = (logical_address % self.row_bytes) // self.cache_line_bytes
        return PhysicalAddress(stack_id, layer_id, kind, channel, pseudo, bank_group, bank, row, column)

    def _transfer_cycles(self, byte_count: int) -> int:
        return math.ceil(byte_count / self.bytes_per_cycle)

    def migration_bytes(self, bytes_fp16: int, source_kind: LayerKind) -> Tuple[int, int]:
        """Physical source/destination bytes; logical bytes always remain FP16.

        K8V4 is assumed resident in Capacity in both directions. Quantization
        throughput is assumed matched to DMA (no extra additive stage latency).
        Scale/zero-point metadata is not included in this initial model.
        """
        if bytes_fp16 <= 0:
            raise ValueError("migration size must be positive")
        if self.quantization_mode not in ("K8V4", "none"):
            raise ValueError("unsupported quantization mode")
        compressed = (math.ceil(bytes_fp16 / self.quantization_ratio)
                      if self.quantization_mode == "K8V4" else bytes_fp16)
        return ((compressed, bytes_fp16) if source_kind is LayerKind.CAPACITY
                else (bytes_fp16, compressed))

    def migrate(
        self,
        request_id: str,
        source: PhysicalAddress,
        target: PhysicalAddress,
        bytes_fp16: int,
        issue_cycle: int = 0,
        priority: int = 0,
    ) -> MigrationResult:
        if bytes_fp16 <= 0:
            raise ValueError("migration size must be positive")
        if source.stack_id != target.stack_id:
            raise ValueError("this Phase 3 model only supports intra-stack migration")
        if source.layer_kind == target.layer_kind:
            raise ValueError("migration must cross Capacity and Compute domains")
        if self._buffer_in_use + min(bytes_fp16, self.buffer_bytes) > self.buffer_bytes:
            raise BufferError("Base Die page buffer is full")
        if priority < 0:
            raise ValueError("priority must be non-negative")

        self._buffer_in_use += min(bytes_fp16, self.buffer_bytes)
        self.stats.max_buffer_bytes = max(self.stats.max_buffer_bytes, self._buffer_in_use)
        start = max(issue_cycle, self._dma_available_cycle)
        queue_cycles = start - issue_cycle
        source_bytes, transferred_bytes = self.migration_bytes(bytes_fp16, source.layer_kind)
        read_cycles = self._transfer_cycles(source_bytes)
        write_cycles = self._transfer_cycles(transferred_bytes)
        quant_cycles = 0  # matched-throughput pipelined approximation, not measured latency
        complete = start + read_cycles + write_cycles + quant_cycles
        self._dma_available_cycle = complete
        self._buffer_in_use -= min(bytes_fp16, self.buffer_bytes)

        is_promotion = source.layer_kind is LayerKind.CAPACITY
        if is_promotion:
            self.stats.promotions += 1
            self.stats.promotion_bytes_fp16 += bytes_fp16
        else:
            self.stats.demotions += 1
            self.stats.demotion_bytes_fp16 += bytes_fp16
        self.stats.transferred_bytes += source_bytes + transferred_bytes
        self.stats.queue_cycles += queue_cycles
        self.stats.transfer_cycles += read_cycles + write_cycles
        self.stats.quantization_cycles += quant_cycles
        self.stats.last_complete_cycle = max(self.stats.last_complete_cycle, complete)
        return MigrationResult(
            request_id,
            start,
            complete,
            source_bytes,
            transferred_bytes,
            queue_cycles,
            read_cycles,
            write_cycles,
            quant_cycles,
            self.stats.max_buffer_bytes,
        )


_PIM_DURATIONS = {
    "PIM_WR_GB": 4,
    "PIM_MV_SB": 4,
    "PIM_MV_GB": 4,
    "PIM_SFM": 16,
    "PIM_MAC_AB": 8,
    "PIM_MAC_SB": 8,
    "PIM_MAC_PB": 8,
    "PIM_BARRIER": 1,
    "PIM_SET_MODEL": 1,
    "PIM_SET_HEAD": 1,
}


def load_attacc_pim_trace(
    path: Path,
    stack: TokenStack,
    request_bytes: int = 32,
    issue_cycle_stride: int = 1,
) -> List[AttAccPIMRequest]:
    """Parse the two-column PIM trace emitted by AttAcc's trace generators.

    The trace has no timestamps, so this adapter assigns monotonically spaced
    issue cycles. Command durations are the current behavior-model assumptions;
    Ramulator2 remains the source of detailed PIM timing.
    """
    if request_bytes <= 0 or issue_cycle_stride <= 0:
        raise ValueError("request bytes and issue stride must be positive")
    requests: List[AttAccPIMRequest] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2 or fields[0] not in _PIM_DURATIONS:
            raise ValueError(f"invalid AttAcc PIM trace line {line_number + 1}: {raw_line}")
        command, raw_address = fields
        address = int(raw_address, 0)
        physical = stack.map_address(address - (address % stack.cache_line_bytes), LayerKind.COMPUTE)
        requests.append(
            AttAccPIMRequest(
                request_id=f"pim-{len(requests)}",
                command=command,
                address=address,
                physical=physical,
                bytes=request_bytes,
                issue_cycle=len(requests) * issue_cycle_stride,
                duration_cycles=_PIM_DURATIONS[command],
            )
        )
    return requests


class ResourceArbiter:
    """Shared Channel/Base Die/TSV resource arbiter for Phase 3.

    Requests are accepted in issue-time and priority order. A request reserves
    all resources it needs for its service interval. This is deliberately a
    conservative non-preemptive model: it exposes contention and completion
    ordering without claiming a particular HBM4 command scheduler.
    """

    def __init__(self, stack: TokenStack, pim_command_bytes: int = 32) -> None:
        if pim_command_bytes <= 0:
            raise ValueError("PIM command bytes must be positive")
        self.stack = stack
        self.pim_command_bytes = pim_command_bytes
        self.channel_available = [0] * stack.channels_per_stack
        self.base_die_available = 0
        self.tsv_available = 0
        self.stats = ArbitrationStats()

    def _dma_cycles(self, bytes_count: int) -> int:
        return math.ceil(bytes_count / self.stack.bytes_per_cycle)

    def _channels_for(self, physical: PhysicalAddress) -> Tuple[int, ...]:
        return (physical.channel,)

    def _reserve(self, channels: Tuple[int, ...], issue_cycle: int, duration: int,
                 base_die: bool, tsv: bool) -> Tuple[int, int, int]:
        start = max(issue_cycle, *(self.channel_available[channel] for channel in channels))
        if base_die:
            start = max(start, self.base_die_available)
        if tsv:
            start = max(start, self.tsv_available)
        complete = start + duration
        for channel in channels:
            self.channel_available[channel] = complete
        if base_die:
            self.base_die_available = complete
        if tsv:
            self.tsv_available = complete
        return start, complete, start - issue_cycle

    def _schedule_pim(self, request: AttAccPIMRequest) -> ArbitrationResult:
        channels = self._channels_for(request.physical)
        start, complete, queue = self._reserve(
            channels, request.issue_cycle, request.duration_cycles, base_die=True, tsv=False
        )
        self.stats.pim_requests += 1
        self.stats.pim_command_bytes += request.bytes
        self.stats.queue_cycles += queue
        self.stats.base_die_busy_cycles += request.duration_cycles
        self.stats.last_complete_cycle = max(self.stats.last_complete_cycle, complete)
        return ArbitrationResult(
            request.request_id, "pim", start, complete, queue, channels, request.duration_cycles, 0,
            request.bytes, request.bytes, request.bytes, request.bytes, request.command
        )

    def _schedule_migration(self, request: MigrationSpec) -> ArbitrationResult:
        if request.source.stack_id != request.target.stack_id:
            raise ValueError("the shared arbiter currently supports intra-stack migration only")
        if request.source.layer_kind == request.target.layer_kind:
            raise ValueError("migration must cross Capacity and Compute domains")
        if request.bytes_fp16 <= 0:
            raise ValueError("migration size must be positive")
        source_bytes, transferred_bytes = self.stack.migration_bytes(
            request.bytes_fp16, request.source.layer_kind
        )
        read_cycles = self._dma_cycles(source_bytes)
        write_cycles = self._dma_cycles(transferred_bytes)
        quant_cycles = 0  # matched-throughput pipeline; no invented per-byte compute delay
        dma_cycles = read_cycles + write_cycles + quant_cycles
        channels = tuple(sorted(set(self._channels_for(request.source) + self._channels_for(request.target))))
        start, complete, queue = self._reserve(
            channels, request.issue_cycle, dma_cycles, base_die=True, tsv=True
        )
        self.stats.migration_requests += 1
        self.stats.migration_logical_read_bytes += request.bytes_fp16
        self.stats.migration_logical_write_bytes += request.bytes_fp16
        self.stats.migration_physical_read_bytes += source_bytes
        self.stats.migration_physical_write_bytes += transferred_bytes
        self.stats.queue_cycles += queue
        self.stats.base_die_busy_cycles += dma_cycles
        self.stats.tsv_busy_cycles += dma_cycles
        self.stats.last_complete_cycle = max(self.stats.last_complete_cycle, complete)
        return ArbitrationResult(
            request.request_id, "migration", start, complete, queue, channels,
            dma_cycles, dma_cycles, request.bytes_fp16, request.bytes_fp16,
            source_bytes, transferred_bytes
        )

    def run(
        self,
        pim_requests: Iterable[AttAccPIMRequest],
        migrations: Iterable[MigrationSpec],
    ) -> List[ArbitrationResult]:
        events = [(request.issue_cycle, request.priority, 0, request) for request in pim_requests]
        events.extend((request.issue_cycle, request.priority, 1, request) for request in migrations)
        events.sort(key=lambda event: (event[0], event[1], event[2]))
        results: List[ArbitrationResult] = []
        for _, _, _, request in events:
            if isinstance(request, AttAccPIMRequest):
                results.append(self._schedule_pim(request))
            else:
                results.append(self._schedule_migration(request))
        return results

    def conservation(self, results: Iterable[ArbitrationResult]) -> Dict[str, int | bool]:
        rows = list(results)
        migration_rows = [row for row in rows if row.request_type == "migration"]
        pim_rows = [row for row in rows if row.request_type == "pim"]
        logical_read = sum(row.logical_read_bytes for row in migration_rows)
        logical_write = sum(row.logical_write_bytes for row in migration_rows)
        physical_read = sum(row.physical_read_bytes for row in migration_rows)
        physical_write = sum(row.physical_write_bytes for row in migration_rows)
        max_complete = max((row.complete_cycle for row in rows), default=0)
        return {
            "pim_request_count": len(pim_rows),
            "migration_request_count": len(migration_rows),
            "pim_command_bytes": sum(row.logical_read_bytes for row in pim_rows),
            "migration_logical_read_bytes": logical_read,
            "migration_logical_write_bytes": logical_write,
            "migration_physical_read_bytes": physical_read,
            "migration_physical_write_bytes": physical_write,
            "logical_migration_bytes_conserved": logical_read == logical_write,
            "completion_cycle_matches_max_event": max_complete == self.stats.last_complete_cycle,
        }


def load_topology(path: str) -> TokenStack:
    return TokenStack.from_yaml(Path(path))

