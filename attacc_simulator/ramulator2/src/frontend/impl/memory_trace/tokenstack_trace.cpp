#include <algorithm>
#include <array>
#include <cmath>
#include <fstream>
#include <limits>
#include <set>

#include "frontend/frontend.h"
#include "addr_mapper/addr_mapper.h"
#include "dram_controller/controller.h"
#include "dram_controller/tokenstack_controller.h"
#include "memory_system/tokenstack_memory.h"

namespace Ramulator {

// Closed-loop, conservative transaction-level Base Die driver. DRAM commands
// still use the original cycle-level HBM3-PIM controller/device. Supports the
// legacy single domain or an optional two-domain TokenStackMemory backend.
class TokenStackTrace final : public IFrontEnd, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IFrontEnd, TokenStackTrace, "TokenStackTrace",
                                    "Single-channel TokenStack closed-loop trace driver.");
  private:
    struct Operation { int type; Addr_t address; };
    struct Job {
      std::string id, kind;
      Clk_t issue = 0, eligible = -1, admitted = -1, start = -1, complete = -1;
      std::vector<std::string> dependencies;
      std::vector<Operation> operations;
      size_t logical = 0, read_bytes = 0, write_bytes = 0;
      Addr_t source = 0, target = 0;
      int state = 0;  // 0 unreleased, 1 queued, 2 active, 3 done
      int priority() const { return kind == "attention" || kind == "access" ? 0 : kind == "promotion" ? 1 : 2; }
    };
    enum class Phase { PIM, Read, SourceLink, TargetLink, Write };
    std::vector<Job> m_jobs;
    std::vector<size_t> m_queue;
    int m_active = -1;
    Phase m_phase = Phase::PIM;
    size_t m_sent = 0, m_returned = 0, m_expected = 0, m_done = 0;
    int m_next_request_id = 0;
    size_t m_buffer_bytes = 0, m_buffer_peak = 0, m_queue_peak = 0;
    size_t m_buffer_capacity = 0, m_queue_depth = 0, m_burst_bytes = 0;
    size_t m_compressed_num = 0, m_compressed_den = 0;
    Addr_t m_capacity_base = 0, m_compute_base = 0, m_region_bytes = 0;
    Clk_t m_link_until = -1, m_max_cycles = 0;
    int m_tck_ps = 0, m_read_latency = 0, m_write_latency = 0;
    double m_link_GBps = 0;
    size_t m_admission_block_cycles = 0, m_send_retries = 0;
    size_t m_actual_block_cycles = 0;
    IDRAM* m_dram = nullptr;
    IAddrMapper* m_mapper = nullptr;
    IDRAMController* m_controller = nullptr;
    ITokenStackController* m_gate = nullptr;
    ITokenStackMemory* m_layers = nullptr;
    std::array<IDRAM*, 2> m_layer_drams{};
    std::array<IDRAMController*, 2> m_layer_controllers{};
    std::ofstream m_requests, m_job_events, m_resources;

    static void require(bool condition, const std::string& message) {
      if (!condition) throw ConfigurationError("TokenStack: {}", message);
    }

    static int operation_type(const std::string& op) {
      static const std::map<std::string, int> types = {
        {"PIM_MAC_AB", 4}, {"PIM_MAC_SB", 5}, {"PIM_MAC_PB", 6},
        {"PIM_WR_GB", 7}, {"PIM_MV_SB", 8}, {"PIM_MV_GB", 9},
        {"PIM_SFM", 10}, {"PIM_SET_MODEL", 11}, {"PIM_SET_HEAD", 12},
        {"PIM_BARRIER", 13}};
      auto it = types.find(op);
      require(it != types.end(), "unsupported attention operation " + op);
      return it->second;
    }

    void open_log(std::ofstream& out, const std::string& key, const std::string& header) {
      out.open(param<std::string>(key).required());
      require(out.is_open(), "cannot open " + key);
      if (!header.empty()) out << header << '\n';
    }

  public:
    void init() override {
      m_clock_ratio = param<uint>("clock_ratio").required();
      m_buffer_capacity = param<size_t>("buffer_bytes").required();
      m_queue_depth = param<size_t>("queue_depth").required();
      m_burst_bytes = param<size_t>("burst_bytes").required();
      m_capacity_base = param<Addr_t>("capacity_base").required();
      m_compute_base = param<Addr_t>("compute_base").required();
      m_region_bytes = param<Addr_t>("region_bytes").required();
      m_compressed_num = param<size_t>("compressed_num").required();
      m_compressed_den = param<size_t>("compressed_den").required();
      m_link_GBps = param<double>("base_die_link_GBps").required();
      m_max_cycles = param<Clk_t>("max_cycles").required();
      require(m_clock_ratio == 1 && m_queue_depth > 0 && m_buffer_capacity > 0
              && m_burst_bytes > 0 && m_max_cycles > 0, "invalid clock/queue/buffer/burst/timeout");
      require(m_compressed_num > 0 && m_compressed_num <= m_compressed_den,
              "invalid compressed byte ratio");
      require(std::isfinite(m_link_GBps) && m_link_GBps > 0, "invalid link bandwidth");
      require(m_capacity_base >= 0 && m_compute_base >= 0 && m_region_bytes > 0,
              "invalid layer regions");
      require(m_region_bytes <= std::numeric_limits<Addr_t>::max() - std::max(m_capacity_base, m_compute_base),
              "layer region overflow");
      require(m_capacity_base + m_region_bytes <= m_compute_base
              || m_compute_base + m_region_bytes <= m_capacity_base, "layer regions overlap");
      require(m_capacity_base % m_burst_bytes == 0 && m_compute_base % m_burst_bytes == 0
              && m_region_bytes % m_burst_bytes == 0, "unaligned layer region");

      auto trace = YAML::LoadFile(param<std::string>("path").required());
      require(trace["schema_version"].as<int>() == 1 && trace["jobs"].IsSequence(), "unsupported workload schema");
      std::set<std::string> ids;
      for (auto entry : trace["jobs"]) {
        Job job;
        job.id = entry["id"].as<std::string>();
        require(!job.id.empty() && job.id.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
                == std::string::npos && ids.insert(job.id).second, "invalid/duplicate job id");
        job.kind = entry["kind"].as<std::string>();
        require(job.kind == "attention" || job.kind == "access" || job.kind == "promotion" || job.kind == "demotion", "invalid job kind");
        job.issue = entry["issue_cycle"].as<Clk_t>();
        require(job.issue >= 0 && job.issue < m_max_cycles, "issue cycle out of range");
        if (entry["depends_on"]) job.dependencies = entry["depends_on"].as<std::vector<std::string>>();
        if (job.kind == "attention" || job.kind == "access") {
          require(entry["requests"].IsSequence() && entry["requests"].size() > 0, "empty attention requests");
          std::string layer = job.kind == "access" ? entry["layer"].as<std::string>() : "compute";
          require(layer == "capacity" || layer == "compute", "invalid access layer");
          // One operation per diagnostic access job makes dependency ordering
          // explicit and avoids interpreting read-after-write forwarding as RD.
          require(job.kind != "access" || entry["requests"].size() == 1, "access job requires one request");
          for (auto request : entry["requests"]) {
            Addr_t offset = request["offset"].as<Addr_t>();
            require(offset >= 0 && offset < m_region_bytes && offset % m_burst_bytes == 0, "invalid PIM offset");
            std::string op = request["op"].as<std::string>();
            int type = job.kind == "access" ? (op == "LD" ? 0 : op == "ST" ? 1 : -1) : operation_type(op);
            require(type >= 0, "unsupported access operation");
            job.operations.push_back({type, (layer == "capacity" ? m_capacity_base : m_compute_base) + offset});
          }
        } else {
          job.logical = entry["logical_bytes"].as<size_t>();
          require(job.logical > 0 && job.logical <= m_buffer_capacity && job.logical <= size_t(m_region_bytes),
                  "migration exceeds buffer/region or is empty (chunking unsupported)");
          require(job.logical <= std::numeric_limits<size_t>::max() / m_compressed_num
                  && (job.logical * m_compressed_num) % m_compressed_den == 0, "fractional/overflow compressed bytes");
          size_t compressed = job.logical * m_compressed_num / m_compressed_den;
          job.read_bytes = job.kind == "promotion" ? compressed : job.logical;
          job.write_bytes = job.kind == "promotion" ? job.logical : compressed;
          Addr_t offset = entry["offset"].as<Addr_t>();
          require(offset >= 0 && offset <= m_region_bytes - Addr_t(job.logical)
                  && offset % m_burst_bytes == 0 && job.read_bytes % m_burst_bytes == 0
                  && job.write_bytes % m_burst_bytes == 0, "migration alignment/bounds failure");
          job.source = (job.kind == "promotion" ? m_capacity_base : m_compute_base) + offset;
          job.target = (job.kind == "promotion" ? m_compute_base : m_capacity_base) + offset;
        }
        m_jobs.push_back(std::move(job));
      }
      require(!m_jobs.empty(), "empty workload");
      // Reject missing dependencies and cycles, instead of hanging at runtime.
      std::set<std::string> reachable;
      for (size_t pass = 0; pass < m_jobs.size(); ++pass) {
        for (const auto& job : m_jobs) {
          for (const auto& dep : job.dependencies) require(ids.count(dep), "unknown dependency " + dep);
          if (std::all_of(job.dependencies.begin(), job.dependencies.end(),
                          [&](const auto& dep) { return reachable.count(dep); })) reachable.insert(job.id);
        }
      }
      require(reachable.size() == m_jobs.size(), "dependency cycle");
      open_log(m_requests, "request_log", "");  // schema chosen after backend setup
      open_log(m_job_events, "job_log", "schema_version,job_id,kind,issue_cycle,eligible_cycle,admit_cycle,start_cycle,complete_cycle,logical_bytes,read_bytes,write_bytes");
      open_log(m_resources, "resource_log", "schema_version,job_id,resource,start_cycle,end_cycle,bytes");
      register_stat(m_buffer_peak).name("tokenstack_buffer_peak_bytes");
      register_stat(m_queue_peak).name("tokenstack_queue_peak_jobs");
      register_stat(m_admission_block_cycles).name("tokenstack_admission_block_cycles");
      register_stat(m_send_retries).name("tokenstack_send_retries");
      register_stat(m_tck_ps).name("tokenstack_tck_ps");
      register_stat(m_actual_block_cycles).name("tokenstack_controller_block_cycles");
      register_stat(m_read_latency).name("tokenstack_read_latency_cycles");
      register_stat(m_write_latency).name("tokenstack_write_recovery_cycles");
    }

    void setup(IFrontEnd*, IMemorySystem* memory) override {
      m_layers = dynamic_cast<ITokenStackMemory*>(memory);
      require(memory->get_clock_ratio() == 1 && (m_layers || memory->m_impl->get_name() == "PIMDRAM"), "requires PIMDRAM or TokenStackMemory at 1:1 clock");
      if (m_layers) require(m_layers->layer_base(0) == m_capacity_base && m_layers->layer_base(1) == m_compute_base
                            && m_layers->region_bytes() == m_region_bytes, "frontend/backend layer map mismatch");
      for (int layer = 0; layer < 2; ++layer) {
        auto* backend = m_layers ? m_layers->layer_memory(layer) : memory;
        auto* dram = backend->get_ifce<IDRAM>();
        auto* mapper = backend->get_ifce<IAddrMapper>();
        m_layer_drams[layer] = dram;
        m_layer_controllers[layer] = backend->get_ifce<IDRAMController>();
        require(dram->get_level_size("channel") == 1 && mapper->m_impl->get_name() == "HBM3-PIM", "only single-channel HBM3-PIM mapping supported");
        require(m_burst_bytes == size_t(dram->m_internal_prefetch_size * dram->m_channel_width / 8), "burst_bytes differs from DRAM transfer size");
      }
      m_dram = m_layer_drams[1];
      m_controller = m_layer_controllers[1];
      m_mapper = (m_layers ? m_layers->layer_memory(1) : memory)->get_ifce<IAddrMapper>();
      m_gate = m_layers ? static_cast<ITokenStackController*>(m_layers) : dynamic_cast<ITokenStackController*>(m_controller);
      require(m_gate && m_gate->tokenstack_enabled(), "requires enabled closed-loop HBM3-PIM controller");
      require(m_dram->get_level_size("channel") == 1 && m_mapper->m_impl->get_name() == "HBM3-PIM",
              "only single-channel HBM3-PIM mapping supported");
      require(m_burst_bytes == size_t(m_dram->m_internal_prefetch_size * m_dram->m_channel_width / 8),
              "burst_bytes differs from DRAM transfer size");
      // This mapper drops upper address bits. Check complete physical span so
      // disjoint logical windows cannot silently alias after decode.
      size_t span = m_burst_bytes;
      for (int count : m_dram->m_organization.count) {
        require(count > 0 && (count & (count - 1)) == 0
                && span <= size_t(std::numeric_limits<Addr_t>::max()) / size_t(count), "unsupported DRAM geometry");
        span *= count;
      }
      require(m_layers || size_t(std::max(m_capacity_base, m_compute_base) + m_region_bytes) <= span, "region exceeds mapped DRAM span");
      m_tck_ps = std::lround(memory->get_tCK() * 1000.0f);
      require(m_tck_ps > 0, "invalid DRAM clock");
      m_read_latency = m_dram->m_read_latency;
      m_write_latency = m_dram->m_timing_vals("nCWL") + m_dram->m_timing_vals("nBL") + m_dram->m_timing_vals("nWR");
      m_requests << "schema_version,request_id,job_id,phase,index,type_id,address,channel,accept_cycle,command_cycle,event_cycle,callback_cycle,bytes,event";
      if (m_layers) m_requests << ",layer_id,local_address,tck_ps";
      m_requests << '\n';
    }

    void tick() override {
      require(m_clk < m_max_cycles, "simulation timeout");
      admit_jobs();
      if (m_active < 0 && !m_queue.empty()) {
        auto best = std::min_element(m_queue.begin(), m_queue.end(), [&](size_t a, size_t b) { return precedes(a, b); });
        m_active = int(*best);
        m_queue.erase(best);
        auto& job = m_jobs[m_active];
        job.state = 2;
        job.start = m_clk;
        m_buffer_bytes = job.logical;
        m_buffer_peak = std::max(m_buffer_peak, m_buffer_bytes);
        begin_phase(job.kind == "attention" || job.kind == "access" ? Phase::PIM : Phase::Read);
      }
      if (m_active >= 0) progress();
      ++m_clk;  // At next frontend call this equals the controller's boundary.
    }

    bool is_finished() override { return m_done == m_jobs.size(); }

    void finalize() override {
      require(is_finished() && m_buffer_bytes == 0, "undrained jobs/buffer");
      m_actual_block_cycles = m_gate->blocked_cycles();
      m_requests.flush(); m_job_events.flush(); m_resources.flush();
      IFrontEnd::finalize();
    }

  private:
    bool precedes(size_t a, size_t b) const {
      const auto& x = m_jobs[a]; const auto& y = m_jobs[b];
      return std::make_tuple(x.priority(), x.issue, a) < std::make_tuple(y.priority(), y.issue, b);
    }

    void admit_jobs() {
      std::vector<size_t> ready;
      for (size_t i = 0; i < m_jobs.size(); ++i) {
        auto& job = m_jobs[i];
        if (job.state || job.issue > m_clk) continue;
        bool dependencies_done = std::all_of(job.dependencies.begin(), job.dependencies.end(), [&](const auto& dep) {
          return std::any_of(m_jobs.begin(), m_jobs.end(), [&](const Job& other) { return other.id == dep && other.state == 3; });
        });
        if (!dependencies_done) continue;
        if (job.eligible < 0) job.eligible = m_clk;
        ready.push_back(i);
      }
      std::sort(ready.begin(), ready.end(), [&](size_t a, size_t b) { return precedes(a, b); });
      for (size_t index : ready) {
        if (m_queue.size() == m_queue_depth) { ++m_admission_block_cycles; break; }
        m_jobs[index].state = 1; m_jobs[index].admitted = m_clk;
        m_queue.push_back(index);
        m_queue_peak = std::max(m_queue_peak, m_queue.size());
      }
    }

    void begin_phase(Phase phase) {
      m_phase = phase; m_sent = 0; m_returned = 0;
      auto& job = m_jobs[m_active];
      m_expected = phase == Phase::PIM ? job.operations.size()
                   : (phase == Phase::Read ? job.read_bytes : job.write_bytes) / m_burst_bytes;
    }

    void start_link(Phase phase, size_t bytes) {
      m_phase = phase;
      // Additional serial Base Die/TSV hop, distinct from the DRAM nBL data
      // path. Conservative, no overlap. Quant/dequant assumed throughput matched.
      double duration = std::ceil(double(bytes) * 1000.0 / (m_link_GBps * m_tck_ps));
      require(duration >= 1 && duration <= double(m_max_cycles - m_clk), "link exceeds time budget");
      m_link_until = m_clk + Clk_t(duration);
      m_gate->block_commands_until(m_link_until);
      if (phase == Phase::TargetLink) {
        // DMA descriptors may enter the controller before data arrives. The
        // controller gate must prevent ACT/WR issue until the transfer ends.
        m_sent = 0; m_returned = 0;
        m_expected = m_jobs[m_active].write_bytes / m_burst_bytes;
      }
      m_resources << "1," << m_jobs[m_active].id << ','
                  << (phase == Phase::SourceLink ? "source_tsv_base_die" : "target_tsv_base_die")
                  << ',' << m_clk << ',' << m_link_until << ',' << bytes << '\n';
    }

    void progress() {
      auto& job = m_jobs[m_active];
      if (m_phase == Phase::Read && m_returned == m_expected) start_link(Phase::SourceLink, job.read_bytes);
      if (m_phase == Phase::SourceLink && m_clk >= m_link_until) start_link(Phase::TargetLink, job.write_bytes);
      if (m_phase == Phase::TargetLink && m_clk >= m_link_until) m_phase = Phase::Write;
      if (m_phase == Phase::SourceLink) return;

      while (m_sent < m_expected) {
        size_t index = m_sent;
        bool pim = m_phase == Phase::PIM;
        int type = pim ? job.operations[index].type : m_phase == Phase::Read ? Request::Type::Read : Request::Type::Write;
        Addr_t address = pim ? job.operations[index].address
                            : (m_phase == Phase::Read ? job.source : job.target) + index * m_burst_bytes;
        Request req(address, type);
        req.source_id = m_next_request_id;
        auto phase = m_phase == Phase::TargetLink ? Phase::Write : m_phase;
        req.callback = [this, index, phase](Request& completed) { returned(completed, index, phase); };
        if (!m_memory_system->send(req)) { ++m_send_retries; break; }
        ++m_sent; ++m_next_request_id;
      }
    }

    void returned(Request& req, size_t index, Phase phase) {
      require(m_active >= 0 && m_phase == phase, "callback in wrong phase");
      auto& job = m_jobs[m_active];
      int layer = m_layers ? m_layers->route(req.addr) : 1;
      auto* dram = m_layer_drams[layer];
      bool pim = req.type_id >= 4;
      int latency = pim ? 0 : req.type_id == Request::Type::Read ? dram->m_read_latency
                            : dram->m_timing_vals("nCWL") + dram->m_timing_vals("nBL") + dram->m_timing_vals("nWR");
      const char* event = pim ? (req.type_id == Request::Type::PIM_BARRIER ? "barrier_removed" : "pim_final_command_issued")
                             : req.type_id == Request::Type::Read ? "read_data_ready" : "write_recovery_done";
      m_requests << (m_layers ? "2," : "1,") << req.source_id << ',' << job.id << ',' << (phase == Phase::PIM ? job.kind : phase == Phase::Read ? "read" : "write")
                 << ',' << index << ',' << req.type_id << ',' << req.addr << ',' << req.addr_vec[0] << ','
                 << req.arrive << ',' << req.depart - latency << ',' << req.depart << ',' << m_layer_controllers[layer]->get_clk()
                 << ',' << (pim ? 0 : m_burst_bytes) << ',' << event;
      if (m_layers) m_requests << ',' << layer << ',' << req.addr - m_layers->layer_base(layer) << ',' << m_tck_ps;
      m_requests << '\n';
      require(++m_returned <= m_expected, "duplicate callback");
      if (m_returned == m_expected && (phase == Phase::PIM || phase == Phase::Write)) finish_job(req.depart);
    }

    void finish_job(Clk_t complete) {
      auto& job = m_jobs[m_active];
      job.complete = complete; job.state = 3; ++m_done;
      m_job_events << "1," << job.id << ',' << job.kind << ',' << job.issue << ',' << job.eligible << ',' << job.admitted
                   << ',' << job.start << ',' << complete << ',' << job.logical << ',' << job.read_bytes << ',' << job.write_bytes << '\n';
      m_resources << "1," << job.id << ",channel_base_die_transaction," << job.start << ',' << complete << ",0\n";
      m_buffer_bytes = 0; m_active = -1;
    }
};

}  // namespace Ramulator
