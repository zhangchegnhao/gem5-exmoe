#include <filesystem>
#include <iostream>
#include <fstream>
#include <cmath>

#include "frontend/frontend.h"
#include "base/exception.h"

namespace Ramulator {

namespace fs = std::filesystem;

class PIMLoadStoreTrace : public IFrontEnd, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IFrontEnd, PIMLoadStoreTrace, "PIMLoadStoreTrace", "PIM/Load/Store memory address trace.")

  private:
    struct Trace {
      int req_type;
      Addr_t addr;
    };
    std::vector<Trace> m_trace;

    size_t m_trace_length = 0;
    size_t m_curr_trace_idx = 0;

    size_t m_trace_count = 0;

    Logger_t m_logger;
    std::ofstream m_completion_trace;
    int m_tck_ps = 0;

  public:
    void init() override {
      std::string trace_path_str = param<std::string>("path").desc("Path to the load store trace file.").required();
      m_clock_ratio = param<uint>("clock_ratio").required();

      std::string completion_path = param<std::string>("completion_path")
        .desc("Optional PIM request completion trace path.")
        .default_val("");
      if (!completion_path.empty()) {
        fs::path path(completion_path);
        if (!path.parent_path().empty()) {
          fs::create_directories(path.parent_path());
        }
        m_completion_trace.open(path, std::ios::out | std::ios::trunc);
        if (!m_completion_trace.is_open()) {
          throw ConfigurationError("Completion trace {} cannot be opened!", completion_path);
        }
        m_completion_trace << "schema_version,source_id,type_id,address,channel,arrive_cycle,retire_cycle,tck_ps,event\n";
      }

      m_logger = Logging::create_logger("LoadStoreTrace");
      m_logger->info("Loading trace file {} ...", trace_path_str);
      init_trace(trace_path_str);
      m_logger->info("Loaded {} lines.", m_trace.size());
    };

    void setup(IFrontEnd* frontend, IMemorySystem* memory_system) override {
      m_tck_ps = static_cast<int>(std::lround(memory_system->get_tCK() * 1000.0f));
    }

    void finalize() override {
      if (m_completion_trace.is_open()) {
        m_completion_trace.flush();
      }
      IFrontEnd::finalize();
    }

    void tick() override {
      if (is_finished()) {
        return;
      }
      bool req_full = false;
      while(!req_full && !is_finished()) {
        const Trace& t = m_trace[m_curr_trace_idx];
        bool request_sent = false;
        Request request{t.addr, t.req_type};
        request.source_id = static_cast<int>(m_trace_count);
        if (m_completion_trace.is_open()) {
          request.callback = [this](Request& completed) {
            m_completion_trace << "1," << completed.source_id << ","
                               << completed.type_id << ","
                               << completed.addr << ","
                               << completed.addr_vec[0] << ","
                               << completed.arrive << ","
                               << completed.depart << "," << m_tck_ps << ","
                               << (completed.type_id == Request::Type::PIM_BARRIER
                                   ? "barrier_removed" : "final_command_issued") << "\n";
          };
        }
        request_sent = m_memory_system->send(request);
        if (request_sent) {
          m_curr_trace_idx = (m_curr_trace_idx + 1) % m_trace_length;
          m_trace_count++;
        }
        else {
          req_full = true;
        }
      }
    };


  private:
    void init_trace(const std::string& file_path_str) {
      fs::path trace_path(file_path_str);
      if (!fs::exists(trace_path)) {
        throw ConfigurationError("Trace {} does not exist!", file_path_str);
      }

      std::ifstream trace_file(trace_path);
      if (!trace_file.is_open()) {
        throw ConfigurationError("Trace {} cannot be opened!", file_path_str);
      }

      std::string line;
      while (std::getline(trace_file, line)) {
        std::vector<std::string> tokens;
        tokenize(tokens, line, " ");

        // TODO: Add line number here for better error messages
        if (tokens.size() != 2) {
          throw ConfigurationError("Trace {} format invalid!", file_path_str);
        }

        int req_type = -1; 
        if (tokens[0] == "LD") {
          req_type = 0;
        } else if (tokens[0] == "ST") {
          req_type = 1;
        } else if (tokens[0] == "PIM_MAC_AB") {
          req_type = 4;
        } else if (tokens[0] == "PIM_MAC_SB") {
          req_type = 5;
        } else if (tokens[0] == "PIM_MAC_PB") {
          req_type = 6;
        } else if (tokens[0] == "PIM_WR_GB") {
          req_type = 7;
        } else if (tokens[0] == "PIM_MV_SB") {
          req_type = 8;
        } else if (tokens[0] == "PIM_MV_GB") {
          req_type = 9;
        } else if (tokens[0] == "PIM_SFM") {
          req_type = 10;
        } else if (tokens[0] == "PIM_SET_MODEL") {
          req_type = 11;
        } else if (tokens[0] == "PIM_SET_HEAD") {
          req_type = 12;
        } else if (tokens[0] == "PIM_BARRIER") {
          req_type = 13;
        } else {
          throw ConfigurationError("Trace {} format invalid!", file_path_str);
        }

        Addr_t addr = -1;
        if (tokens[1].compare(0, 2, "0x") == 0 | tokens[1].compare(0, 2, "0X") == 0) {
          addr = std::stoll(tokens[1].substr(2), nullptr, 16);
        } else {
          addr = std::stoll(tokens[1]);
        }
        m_trace.push_back({req_type, addr});
      }

      trace_file.close();

      m_trace_length = m_trace.size();
    };

    // TODO: FIXME
    bool is_finished() override {
      return m_trace_count >= m_trace_length; 
    };
};

}        // namespace Ramulator
