#ifndef RAMULATOR_TOKENSTACK_MEMORY_H
#define RAMULATOR_TOKENSTACK_MEMORY_H

#include "memory_system/memory_system.h"
#include "dram_controller/tokenstack_controller.h"

namespace Ramulator {

// Optional two-domain interface. Layer 0 = Capacity, layer 1 = Compute.
class ITokenStackMemory : public ITokenStackController {
  public:
    virtual IMemorySystem* layer_memory(int layer) const = 0;
    virtual Addr_t layer_base(int layer) const = 0;
    virtual Addr_t region_bytes() const = 0;
    virtual int route(Addr_t address) const = 0;
};

}  // namespace Ramulator
#endif
