#ifndef RAMULATOR_TOKENSTACK_FABRIC_H
#define RAMULATOR_TOKENSTACK_FABRIC_H
#include "memory_system/memory_system.h"
namespace Ramulator {
// Optional hardware-fabric API. Times are absolute DRAM clock boundaries.
class ITokenStackFabric {
 public:
  virtual ~ITokenStackFabric() = default;
  virtual bool submit(Request request, const std::string& source) = 0;
  virtual bool work(int tag, int stack, const std::string& resource, size_t bytes,
                    Clk_t cycles, const std::string& source, std::function<void(Clk_t)> done) = 0;
};
}
#endif
