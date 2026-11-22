#include <array>
#include <cmath>
#include <fstream>
#include <limits>
#include <sstream>

#include "memory_system/tokenstack_memory.h"
#include "dram_controller/controller.h"
#include "addr_mapper/addr_mapper.h"

namespace Ramulator {

// Two independent Ramulator device/controller trees, driven on one clock.
// Whole-transaction Base Die ownership remains in TokenStackTrace. This layer
// additionally arbitrates ALL controller issue slots, including refresh, so
// independent devices cannot each consume a full Stack command path per cycle.
class TokenStackMemory final : public IMemorySystem, public Implementation, public ITokenStackMemory {
  RAMULATOR_REGISTER_IMPLEMENTATION(IMemorySystem, TokenStackMemory, "TokenStackMemory",
                                    "Two DRAM timing domains sharing one Stack issue path.");
  private:
    std::array<IMemorySystem*, 2> m_layers{};
    std::array<IDRAMController*, 2> m_controllers{};
    std::array<ITokenStackController*, 2> m_gates{};
    std::array<Addr_t, 2> m_bases{};
    std::array<size_t, 2> m_bursts{};
    std::array<size_t, 2> m_reads{}, m_writes{}, m_pim{}, m_grant_cycles{};
    Addr_t m_region_bytes = 0;
    Clk_t m_clk = 0, m_block_until = -1;
    size_t m_block_cycles = 0, m_read_total = 0, m_write_total = 0;
    size_t m_switch_stalls = 0;
    Clk_t m_switch_guard = 0, m_last_grant_cycle = -1;
    int m_last_grant = 1, m_tck_ps = 0;
    std::ofstream m_grants;

    static void require(bool condition, const std::string& message) {
      if (!condition) throw ConfigurationError("TokenStackMemory: {}", message);
    }

  public:
    void init() override {
      m_clock_ratio = param<uint>("clock_ratio").required();
      require(m_clock_ratio == 1, "requires 1:1 clock ratio");
      m_bases[0] = param<Addr_t>("capacity_base").required();
      m_bases[1] = param<Addr_t>("compute_base").required();
      m_region_bytes = param<Addr_t>("region_bytes").required();
      m_switch_guard = param<Clk_t>("domain_switch_guard_cycles").required();
      require(m_region_bytes > 0 && m_bases[0] >= 0 && m_bases[1] >= 0
              && m_region_bytes <= std::numeric_limits<Addr_t>::max() - std::max(m_bases[0], m_bases[1]), "invalid/overflow region");
      require(m_bases[0] + m_region_bytes <= m_bases[1] || m_bases[1] + m_region_bytes <= m_bases[0], "overlapping regions");
      for (int layer = 0; layer < 2; ++layer) {
        const char* key = layer == 0 ? "Capacity" : "Compute";
        YAML::Node config;
        config["MemorySystem"] = YAML::Clone(m_config[key]);
        require(config["MemorySystem"]["impl"].as<std::string>() == "PIMDRAM", "requires PIMDRAM layer backend");
        // Factory creates a separate top-level component tree. Passing the
        // Stack's tree to controller::setup would bind both layers to DRAM 0.
        m_layers[layer] = Factory::create_memory_system(config);
        auto* dram = m_layers[layer]->get_ifce<IDRAM>();
        auto* mapper = m_layers[layer]->get_ifce<IAddrMapper>();
        m_controllers[layer] = m_layers[layer]->get_ifce<IDRAMController>();
        m_gates[layer] = dynamic_cast<ITokenStackController*>(m_controllers[layer]);
        require(m_layers[layer]->get_clock_ratio() == 1 && dram->m_impl->get_name() == "HBM3-PIM"
                && dram->get_level_size("channel") == 1 && mapper->m_impl->get_name() == "HBM3-PIM"
                && m_gates[layer] && m_gates[layer]->tokenstack_enabled(), "unsupported layer controller/geometry/mapping");
        int tck = std::lround(m_layers[layer]->get_tCK() * 1000.0f);
        require(tck > 0 && (layer == 0 || tck == m_tck_ps), "layer clock mismatch (clock conversion unsupported)");
        m_tck_ps = tck;
        Clk_t safe_bound = std::max<Clk_t>(dram->m_read_latency,
          dram->m_timing_vals("nCWL") + dram->m_timing_vals("nBL") + dram->m_timing_vals("nWR"));
        for (const char* timing : {"nRTW", "nBL", "nCCDAB", "nCCDSB", "nCCDS", "nCCDL"}) {
          safe_bound = std::max<Clk_t>(safe_bound, dram->m_timing_vals(timing));
        }
        for (const char* turnaround : {"nWTRS", "nWTRL"}) {
          safe_bound = std::max<Clk_t>(safe_bound, dram->m_timing_vals("nCWL")
            + dram->m_timing_vals("nBL") + dram->m_timing_vals(turnaround));
        }
        require(m_switch_guard >= safe_bound, "domain switch guard below conservative DRAM drain bound");
        size_t burst = dram->m_internal_prefetch_size * dram->m_channel_width / 8;
        m_bursts[layer] = burst;
        require(burst > 0 && m_bases[layer] % burst == 0 && m_region_bytes % burst == 0, "unaligned region");
        size_t span = burst;
        for (int count : dram->m_organization.count) {
          require(count > 0 && (count & (count - 1)) == 0
                  && span <= size_t(std::numeric_limits<Addr_t>::max()) / size_t(count), "unsupported layer geometry");
          span *= count;
        }
        require(size_t(m_region_bytes) <= span, "region exceeds layer DRAM span");
      }
      require(m_layers[0]->get_ifce<IDRAM>() != m_layers[1]->get_ifce<IDRAM>()
              && m_controllers[0] != m_controllers[1], "aliased timing domains");
      m_grants.open(param<std::string>("grant_log").required());
      require(m_grants.is_open(), "cannot open grant log");
      m_grants << "schema_version,cycle,owner_layer,external_blocked,capacity_pending,compute_pending\n";
      register_stat(m_clk).name("memory_system_cycles");
      register_stat(m_tck_ps).name("stack_tck_ps");
      register_stat(m_read_total).name("total_num_read_requests");
      register_stat(m_write_total).name("total_num_write_requests");
      register_stat(m_block_cycles).name("stack_external_block_cycles");
      register_stat(m_switch_stalls).name("stack_domain_switch_stall_cycles");
      register_stat(m_reads[0]).name("capacity_read_requests");
      register_stat(m_reads[1]).name("compute_read_requests");
      register_stat(m_writes[0]).name("capacity_write_requests");
      register_stat(m_writes[1]).name("compute_write_requests");
      register_stat(m_pim[0]).name("capacity_pim_requests");
      register_stat(m_pim[1]).name("compute_pim_requests_including_barriers");
      register_stat(m_grant_cycles[0]).name("capacity_grant_cycles");
      register_stat(m_grant_cycles[1]).name("compute_grant_cycles");
    }

    void connect_frontend(IFrontEnd* frontend) override {
      IMemorySystem::connect_frontend(frontend);
      for (auto* layer : m_layers) layer->connect_frontend(frontend);
    }

    IMemorySystem* layer_memory(int layer) const override {
      require(layer == 0 || layer == 1, "invalid layer id");
      return m_layers[layer];
    }
    Addr_t layer_base(int layer) const override {
      require(layer == 0 || layer == 1, "invalid layer id");
      return m_bases[layer];
    }
    Addr_t region_bytes() const override { return m_region_bytes; }
    int route(Addr_t address) const override {
      for (int layer = 0; layer < 2; ++layer) {
        if (address >= m_bases[layer] && address - m_bases[layer] < m_region_bytes) return layer;
      }
      throw ConfigurationError("TokenStackMemory: address {} outside layer regions", address);
    }
    bool tokenstack_enabled() const override { return true; }
    size_t blocked_cycles() const override { return m_block_cycles; }
    void block_commands_until(Clk_t boundary) override {
      require(boundary >= m_clk, "invalid shared block boundary");
      m_block_until = std::max(m_block_until, boundary);
    }

    bool send(Request req) override {
      int layer = route(req.addr);
      require(req.type_id == Request::Type::Read || req.type_id == Request::Type::Write
              || (layer == 1 && req.type_id >= 4 && req.type_id <= 13), "PIM is forbidden on Capacity / invalid request type");
      Addr_t global_address = req.addr;
      req.addr -= m_bases[layer];
      require(req.addr % m_bursts[layer] == 0 && m_region_bytes >= Addr_t(m_bursts[layer])
              && req.addr <= m_region_bytes - Addr_t(m_bursts[layer]), "unaligned/out-of-bounds request burst");
      auto callback = req.callback;
      req.callback = [callback, global_address](Request& completed) {
        if (callback) {
          Request visible = completed;
          visible.addr = global_address;  // addr_vec remains the layer-local decode
          callback(visible);
        }
      };
      bool accepted = m_layers[layer]->send(req);
      if (accepted) {
        if (req.type_id == Request::Type::Read) { ++m_reads[layer]; ++m_read_total; }
        else if (req.type_id == Request::Type::Write) { ++m_writes[layer]; ++m_write_total; }
        else ++m_pim[layer];
      }
      return accepted;
    }

    void tick() override {
      ++m_clk;
      std::array<bool, 2> pending = {m_layers[0]->is_pending(), m_layers[1]->is_pending()};
      int owner = -1;
      bool blocked = m_clk <= m_block_until;
      if (blocked) ++m_block_cycles;
      else {
        // Drain the current domain (including read/write callbacks) before
        // switching. A configurable guard also prevents separate DRAM timing
        // tables from bypassing shared bus burst/turnaround constraints. This
        // is deliberately conservative, not a ready-command overlap model.
        if (pending[m_last_grant]) owner = m_last_grant;
        else if (pending[1 - m_last_grant]) owner = 1 - m_last_grant;
        if (owner >= 0 && owner != m_last_grant && m_last_grant_cycle >= 0
            && m_clk - m_last_grant_cycle <= m_switch_guard) {
          owner = -1; ++m_switch_stalls;
        }
      }
      if (owner >= 0) { m_last_grant = owner; m_last_grant_cycle = m_clk; ++m_grant_cycles[owner]; }
      m_grants << "1," << m_clk << ',' << owner << ',' << blocked << ',' << pending[0] << ',' << pending[1] << '\n';
      for (int layer = 0; layer < 2; ++layer) {
        if (layer != owner) m_gates[layer]->block_commands_until(m_clk);
        m_layers[layer]->tick();  // both clocks, read callbacks and refresh tick
      }
    }

    float get_tCK() override { return m_tck_ps / 1000.0f; }
    bool is_pending() override {
      return m_layers[0]->is_pending() || m_layers[1]->is_pending() || m_clk < m_block_until;
    }
    void finalize() override {
      m_grants.flush();
      // The two backends have their own component trees. Finalize each into a
      // YAML string and nest its statistics, rather than duplicating top-level
      // MemorySystem keys in stdout. This also runs backend plugin finalizers.
      std::array<std::string, 2> layer_stats;
      for (int layer = 0; layer < 2; ++layer) {
        std::ostringstream stream;
        auto* original = std::cout.rdbuf(stream.rdbuf());
        try { m_layers[layer]->finalize(); }
        catch (...) { std::cout.rdbuf(original); throw; }
        std::cout.rdbuf(original);
        layer_stats[layer] = stream.str();
      }
      YAML::Emitter emitter;
      emitter << YAML::BeginMap;
      m_impl->print_stats(emitter);
      emitter << YAML::Key << "LayerStats" << YAML::Value << YAML::BeginMap;
      for (int layer = 0; layer < 2; ++layer) {
        emitter << YAML::Key << (layer == 0 ? "capacity" : "compute")
                << YAML::Value << YAML::Load(layer_stats[layer])["MemorySystem"];
      }
      emitter << YAML::EndMap << YAML::EndMap;
      std::cout << emitter.c_str() << std::endl;
    }
};

}  // namespace Ramulator
