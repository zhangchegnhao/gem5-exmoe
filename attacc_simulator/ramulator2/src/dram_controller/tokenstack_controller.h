#ifndef RAMULATOR_TOKENSTACK_CONTROLLER_H
#define RAMULATOR_TOKENSTACK_CONTROLLER_H

#include "base/base.h"
#include "base/request.h"

namespace Ramulator {

// Optional capability, deliberately separate from the generic controller API.
class ITokenStackController {
  public:
    virtual ~ITokenStackController() = default;
    virtual bool tokenstack_enabled() const = 0;
    virtual size_t blocked_cycles() const = 0;
    // Block command issue through this boundary, while clocks, refresh enqueue
    // and already scheduled data-return/recovery callbacks keep progressing.
    virtual void block_commands_until(Clk_t boundary) = 0;
    // Optional atomic command check+reservation, used only by TokenStackFabric.
    // Called after device timing readiness and before plugins/issue, including
    // maintenance and secondary issue. Returning false must not mutate DRAM.
    virtual void set_command_arbiter(std::function<bool(Request&, Clk_t)> arbiter) {
      throw std::runtime_error("Controller does not support command arbitration");
    }
};

}  // namespace Ramulator
#endif
