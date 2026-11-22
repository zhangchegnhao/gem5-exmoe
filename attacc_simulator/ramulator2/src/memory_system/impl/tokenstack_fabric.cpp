#include <algorithm>
#include <cmath>
#include <deque>
#include <fstream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include "memory_system/tokenstack_fabric.h"
#include "dram_controller/controller.h"
#include "dram_controller/tokenstack_controller.h"
#include "addr_mapper/addr_mapper.h"

namespace Ramulator {
// Cycle-driven physical trees; all external timing assumptions are in YAML.
// Resource calendars cover the SAME RD/WR data interval as DRAM nBL, not an
// added pair of links. TSV intervals share a bounded byte rate; PC intervals
// remain exclusive. This is a fluid bandwidth model at DRAM-cycle boundaries.
class TokenStackFabric final : public IMemorySystem, public Implementation, public ITokenStackFabric {
  RAMULATOR_REGISTER_IMPLEMENTATION(IMemorySystem, TokenStackFabric, "TokenStackFabric", "Explicit TokenStack dies and shared resource calendars.");
  struct Layer { int stack,id; std::string kind; Addr_t base,bytes; IMemorySystem* backend; IDRAM* dram; };
  struct Event { Clk_t end; std::function<void(Clk_t)> done; };
  struct Interval { Clk_t start,end; int direction; size_t bytes; };
  struct Reservation { std::string resource; Clk_t start,end; size_t bytes; int direction=0; };
  struct Queued { Request request; size_t layer; std::string source; Clk_t accepted; };
  std::vector<Layer> m_layers;
  std::vector<Event> m_events;
  std::map<std::string,std::vector<Interval>> m_calendar;
  std::map<std::pair<int,std::string>,std::deque<Queued>> m_queues;
  std::map<int,Clk_t> m_data_end, m_issue,m_result_end,m_write_end;
  std::map<std::pair<size_t,int>,Clk_t> m_pim_busy;
  std::map<std::pair<size_t,int>,Clk_t> m_dram_busy;
  std::map<int,std::string> m_source;
  std::map<std::pair<size_t,int>,size_t> m_inflight;
  YAML::Node m_hw;
  Clk_t m_clk=0,m_age=0,m_turn=0;
  size_t m_reads=0,m_writes=0,m_pim=0,m_retries=0,m_stalls=0,m_queue_peak=0;
  int m_tck=0,m_depth=0,m_dispatch_width=0;
  double m_tsv=0,m_ucie=0,m_host=0;
  std::ofstream m_commands,m_resources,m_event_log;
  static void check(bool ok,const std::string& message) { if(!ok) throw ConfigurationError("TokenStackFabric: {}",message); }
  static int priority(const std::string& source) {return source=="gpu"||source=="pim" ? 0 : source=="promotion" ? 1 : 2;}
  std::string stack_key(const std::string& type,int stack) const {return type+"/s"+std::to_string(stack);}
  std::string channel_key(size_t layer,int channel) const {
    return "channel/s"+std::to_string(m_layers[layer].stack)+"/ch"+std::to_string(channel);
  }
  Clk_t transfer(size_t bytes,double gbps) const {
    return std::max<Clk_t>(1,Clk_t(std::ceil(double(bytes)*1000.0/(gbps*m_tck))));
  }
  bool available(const Reservation& r) const {
    const bool shared=r.resource.rfind("tsv/",0)==0;
    const long double limit=m_tsv*m_tck/1000.0L;
    const long double rate=static_cast<long double>(r.bytes)/(r.end-r.start);
    if(shared && rate>limit+1e-9L)return false;
    auto it=m_calendar.find(r.resource); if(it==m_calendar.end()) return true;
    std::map<Clk_t,long double> changes;
    if(shared) {changes[r.start]+=rate;changes[r.end]-=rate;}
    for(const auto& old:it->second) {
      const Clk_t guard=(r.direction && old.direction && r.direction!=old.direction)?m_turn:0;
      const bool opposite=r.direction && old.direction && r.direction!=old.direction;
      if((!shared || opposite) && r.start<old.end+guard && old.start<r.end+guard) return false;
      if(shared && r.start<old.end && old.start<r.end) {
        const long double old_rate=static_cast<long double>(old.bytes)/(old.end-old.start);
        changes[std::max(r.start,old.start)]+=old_rate;
        changes[std::min(r.end,old.end)]-=old_rate;
      }
    }
    long double used=0;
    for(const auto& [cycle,delta]:changes) {used+=delta;if(used>limit+1e-9L)return false;}
    return true;
  }
  bool reserve(const std::vector<Reservation>& requests,int tag,const std::string& source) {
    for(const auto& r:requests) {check(r.end>r.start,"invalid resource interval"); if(!available(r)) return false;}
    for(const auto& r:requests) {
      m_calendar[r.resource].push_back({r.start,r.end,r.direction,r.bytes});
      m_resources<<"1,"<<tag<<','<<source<<','<<r.resource<<','<<r.start<<','<<r.end<<','<<r.bytes<<','<<r.direction<<'\n';
    }
    return true;
  }
  bool command(size_t layer_index,Request& request,Clk_t cycle) {
    auto& layer=m_layers[layer_index];auto* dram=layer.dram;
    const int channel=request.addr_vec[0];
    const std::string channel_resource=channel_key(layer_index,channel);
    const auto unit_key=std::make_pair(layer_index,channel);
    if(m_pim_busy[unit_key]>cycle) {++m_stalls;return false;}
    // The inherited PIM timing table has no complete cross-mode RD/WR->MAC
    // completion relation. Explicit physical-die mode conservatively drains
    // ordinary data/recovery before PIM prerequisites can change bank state.
    if(request.type_id>=4 && m_dram_busy[unit_key]>cycle) {++m_stalls;return false;}
    const auto cmd=std::string(dram->m_commands(request.command));
    const auto source=request.source_id<0 ? "maintenance" : m_source.at(request.source_id);
    std::vector<Reservation> reservations={{channel_resource,cycle,cycle+1,0}};
    Clk_t tail=0;
    if(request.type_id>=4 && request.type_id<=12 && request.command==request.final_command) {
      const char* key=request.type_id<=6 ? "mac_tail_cycles" : request.type_id<=9 ? "move_tail_cycles" : request.type_id==10 ? "softmax_tail_cycles" : "mode_tail_cycles";
      tail=m_hw["pim"][key].as<Clk_t>();check(tail>0,"PIM result tail must be positive and explicit");
      reservations.push_back({"pim/s"+std::to_string(layer.stack)+"/d"+std::to_string(layer.id)+"/ch"+std::to_string(channel),cycle,cycle+tail,0});
      if(request.type_id==10) reservations.push_back({stack_key("coordinator",layer.stack),cycle,cycle+tail,0});
    }
    const bool rd=cmd=="RD",wr=cmd=="WR";
    if(rd||wr) {
      const Clk_t delay=dram->m_timing_vals(rd?"nCL":"nCWL");
      const Clk_t length=std::max<Clk_t>(dram->m_timing_vals("nBL"),transfer(32,m_tsv));
      const int direction=rd?1:2;
      const auto data_start=cycle+delay, data_end=data_start+length;
      reservations.push_back({stack_key("tsv",layer.stack),data_start,data_end,32,direction});
      reservations.push_back({channel_resource+"/pc"+std::to_string(request.addr_vec[1])+"/data",data_start,data_end,32,direction});
      if(!reserve(reservations,request.source_id,source)) {++m_stalls;return false;}
      m_data_end[request.source_id]=data_end;
      const Clk_t recovery=data_end+(wr?dram->m_timing_vals("nWR"):0);
      m_dram_busy[unit_key]=std::max(m_dram_busy[unit_key],recovery);
      if(wr)m_write_end[request.source_id]=recovery;
    } else if(!reserve(reservations,request.source_id,source)) {++m_stalls;return false;}
    if(tail) {m_result_end[request.source_id]=cycle+tail;m_pim_busy[unit_key]=cycle+tail;}
    if(request.source_id>=0 && request.command==request.final_command) m_issue[request.source_id]=cycle;
    m_commands<<"1,"<<cycle<<','<<layer.stack<<','<<layer.id<<','<<request.source_id<<','<<source<<','<<cmd;
    for(int coordinate:request.addr_vec) m_commands<<','<<coordinate;
    m_commands<<'\n'; return true;
  }
  size_t route(Addr_t address) const {
    for(size_t i=0;i<m_layers.size();++i) if(address>=m_layers[i].base && address<m_layers[i].base+m_layers[i].bytes) return i;
    throw ConfigurationError("TokenStackFabric: address outside physical dies");
  }
  void dispatch() {
    // Bounded per-stack source queues; preserve per-channel source order while
    // allowing independent channels to issue through a configurable ingress.
    std::vector<std::pair<int,std::string>> candidates;
    for(auto& [key,queue]:m_queues) if(!queue.empty()) candidates.push_back(key);
    std::stable_sort(candidates.begin(),candidates.end(),[&](const auto& a,const auto& b){
      const auto& x=m_queues.at(a).front();const auto& y=m_queues.at(b).front();
      bool xa=m_clk-x.accepted>=m_age,ya=m_clk-y.accepted>=m_age;
      return std::make_tuple(!xa,xa?0:priority(x.source),x.accepted,a)<std::make_tuple(!ya,ya?0:priority(y.source),y.accepted,b);
    });
    for(auto key:candidates) {
      auto& queue=m_queues.at(key);int admitted=0;
      std::set<std::pair<size_t,int>> blocked;
      for(auto item=queue.begin();item!=queue.end() && admitted<m_dispatch_width;) {
        auto& layer=m_layers[item->layer];Request probe=item->request;
        layer.backend->get_ifce<IAddrMapper>()->apply(probe);
        const auto count_key=std::make_pair(item->layer,probe.addr_vec[0]);
        if(blocked.count(count_key) || m_inflight[count_key]>=size_t(m_depth) || !layer.backend->send(item->request)) {
          blocked.insert(count_key);++item;
        } else {++m_inflight[count_key];++admitted;item=queue.erase(item);}
      }
    }
  }
 public:
  void init() override {
    m_clock_ratio=param<uint>("clock_ratio").required();check(m_clock_ratio==1,"requires 1:1 clocks");
    m_hw=m_config["hardware"];auto resources=m_hw["resources"];
    m_depth=resources["queue_depth_per_source"].as<int>();m_age=resources["aging_cycles"].as<Clk_t>();
    m_dispatch_width=resources["dispatch_per_source_cycle"].as<int>();
    m_turn=resources["tsv_turnaround_cycles"].as<Clk_t>();
    m_tsv=resources["tsv_GBps"].as<double>();m_ucie=resources["ucie_GBps"].as<double>();m_host=resources["host_crossbar_GBps"].as<double>();
    check(m_depth>0 && m_dispatch_width>0 && m_age>0 && m_turn>=0 && std::isfinite(m_tsv) && m_tsv>0 && std::isfinite(m_ucie) && m_ucie>0
          && std::isfinite(m_host) && m_host>0,"invalid queue/link parameters");
    check(resources["tsv_coverage"].as<std::string>()=="dram_data_burst" && resources["command_slots_per_channel_cycle"].as<int>()==1,"unsupported resource profile");
    check(resources["tsv_arbitration"].as<std::string>()=="shared_rate","unsupported TSV arbitration");
    check(m_config["layers"].IsSequence() && m_config["layers"].size()>0,"empty layers");
    std::set<std::pair<int,int>> ids;
    for(auto entry:m_config["layers"]) {
      Layer layer{entry["stack"].as<int>(),entry["die_id"].as<int>(),entry["kind"].as<std::string>(),entry["base"].as<Addr_t>(),entry["bytes"].as<Addr_t>(),nullptr,nullptr};
      check(layer.stack>=0 && layer.stack<m_hw["stacks"].as<int>() && layer.id>=0 && ids.emplace(layer.stack,layer.id).second,"duplicate/invalid layer");
      check(layer.kind=="capacity"||layer.kind=="compute","invalid layer kind");
      check(layer.base>=0 && layer.base%32==0 && layer.bytes>0 && layer.base<=std::numeric_limits<Addr_t>::max()-layer.bytes,"invalid address interval");
      for(const auto& old:m_layers) check(layer.base+layer.bytes<=old.base || old.base+old.bytes<=layer.base,"overlapping physical dies");
      YAML::Node config;config["MemorySystem"]=YAML::Clone(entry["backend"]);layer.backend=Factory::create_memory_system(config);
      check(entry["backend"]["DRAM"]["tokenstack_physical_dies"].as<bool>(false),"physical-die broadcast checks must be enabled");
      layer.dram=layer.backend->get_ifce<IDRAM>();auto* dram=layer.dram;
      check(dram->m_impl->get_name()=="HBM3-PIM" && layer.backend->get_clock_ratio()==1,"unsupported backend");
      size_t span=dram->m_internal_prefetch_size*dram->m_channel_width/8;check(span==32,"requires 32-byte burst");
      for(int count:dram->m_organization.count) {check(count>0 && (count&(count-1))==0 && span<=size_t(std::numeric_limits<Addr_t>::max())/count,"invalid geometry");span*=count;}
      check(size_t(layer.bytes)==span,"capacity/geometry disagreement");
      int clock=std::lround(layer.backend->get_tCK()*1000);check(m_tck==0||m_tck==clock,"clock mismatch");m_tck=clock;
      m_layers.push_back(layer);
    }
    m_commands.open(param<std::string>("command_log").required());m_resources.open(param<std::string>("resource_log").required());m_event_log.open(param<std::string>("event_log").required());
    check(m_commands.is_open()&&m_resources.is_open()&&m_event_log.is_open(),"cannot open fabric logs");
    m_commands<<"schema_version,cycle,stack,die_id,request_id,source,command,channel,pseudochannel,rank,bankgroup,bank,row,column\n";
    m_resources<<"schema_version,tag,source,resource,start_cycle,end_cycle,bytes,direction\n";
    m_event_log<<"schema_version,request_id,type,stack,die_id,accept_cycle,issue_cycle,operand_ready_cycle,data_ready_cycle,result_ready_cycle,complete_cycle\n";
    register_stat(m_clk).name("memory_system_cycles");register_stat(m_reads).name("total_num_read_requests");register_stat(m_writes).name("total_num_write_requests");register_stat(m_pim).name("total_num_pim_requests");
    register_stat(m_stalls).name("fabric_command_resource_stalls");register_stat(m_queue_peak).name("fabric_source_queue_peak");register_stat(m_retries).name("fabric_queue_backpressure");
  }
  void connect_frontend(IFrontEnd* frontend) override {
    IMemorySystem::connect_frontend(frontend);
    for(size_t i=0;i<m_layers.size();++i) {
      auto& layer=m_layers[i];layer.backend->connect_frontend(frontend);
      for(int ch=0;ch<layer.dram->get_level_size("channel");++ch) {
        auto* controller=layer.backend->get_ifce<IDRAMController>("Channel "+std::to_string(ch));
        auto* gate=dynamic_cast<ITokenStackController*>(controller);check(gate,"controller lacks arbitration capability");
        gate->set_command_arbiter([this,i](Request& request,Clk_t cycle){return command(i,request,cycle);});
      }
    }
  }
  bool send(Request req) override {return submit(req,req.type_id<2?"gpu":"pim");}
  bool submit(Request req,const std::string& source) override {
    check(source=="gpu"||source=="pim"||source=="promotion"||source=="demotion","invalid request source");
    const auto layer_index=route(req.addr);auto& layer=m_layers[layer_index];
    check(req.addr%32==0 && req.addr<=layer.base+layer.bytes-32,"unaligned/out-of-bounds burst");
    check(req.type_id==0||req.type_id==1||(req.type_id>=4 && req.type_id<=13 && layer.kind=="compute"),"PIM forbidden on Capacity / invalid type");
    auto& queue=m_queues[{layer.stack,source}];if(queue.size()>=size_t(m_depth)) {++m_retries;return false;}
    check(req.source_id>=0 && !m_source.count(req.source_id),"request ID reused or absent");
    const auto callback=req.callback;const auto global=req.addr;const auto accepted=m_clk;const auto id=req.source_id;
    req.addr-=layer.base;m_source[id]=source;
    req.callback=[this,callback,global,accepted,layer_index,id](Request& completed){
      Request visible=completed;visible.addr=global;
      const auto key=std::make_pair(layer_index,completed.addr_vec[0]);check(m_inflight[key]>0,"inflight underflow");
      --m_inflight[key];
      const auto issue=m_issue.count(id)?m_issue.at(id):completed.depart;
      // A scheduler barrier cannot release before already issued PIM results.
      const auto result=completed.type_id==13 ? std::max(completed.depart,m_pim_busy[key]) : m_result_end.count(id)?m_result_end.at(id):-1;
      const auto end=std::max({completed.depart,m_data_end.count(id)?m_data_end.at(id):completed.depart,result,
                              m_write_end.count(id)?m_write_end.at(id):completed.depart});
      const auto data=m_data_end.count(id)?m_data_end.at(id):-1;
      const auto& layer=m_layers[layer_index];
      m_event_log<<"1,"<<id<<','<<completed.type_id<<','<<layer.stack<<','<<layer.id<<','<<accepted<<','<<issue<<','
                 <<(completed.type_id>=4?issue:-1)<<','<<data<<','<<result<<','<<end<<'\n';
      visible.depart=end;
      if(end>m_clk) m_events.push_back({end,[callback,visible](Clk_t) mutable {if(callback)callback(visible);}});
      else if(callback)callback(visible);
    };
    queue.push_back({req,layer_index,source,accepted});m_queue_peak=std::max(m_queue_peak,queue.size());
    if(req.type_id==0)++m_reads;else if(req.type_id==1)++m_writes;else ++m_pim;
    return true;
  }
  bool work(int tag,int stack,const std::string& resource,size_t bytes,Clk_t cycles,const std::string& source,std::function<void(Clk_t)> done) override {
    check(stack>=0 && stack<m_hw["stacks"].as<int>() && cycles>=0,"invalid work stack/duration");
    check(source=="gpu"||source=="pim"||source=="promotion"||source=="demotion","invalid work source");
    double bandwidth=0;std::string key;bool tsv=false;
    if(resource=="ucie") {key=stack_key("ucie",stack);bandwidth=m_ucie;}
    else if(resource=="host") {key="gpu_crossbar";bandwidth=m_host;}
    else if(resource=="convert") {key=stack_key("convert",stack);bandwidth=m_hw["base_die"]["conversion_GBps"].as<double>();}
    else if(resource=="transpose") {key=stack_key("layout",stack);bandwidth=m_hw["base_die"]["transpose_GBps"].as<double>();}
    else if(resource=="coordinator") {key=stack_key("coordinator",stack);bandwidth=m_hw["pim"]["coordinator_GBps"].as<double>();tsv=true;}
    else if(resource=="gpu_compute") {
      check(m_hw["gpu_system"] && m_hw["gpu_system"]["model"].as<std::string>()=="A100a"
            && m_hw["gpu_system"]["devices"].as<int>()==1,"GPU work requires explicit single-card A100a profile");
      check(source=="gpu" && bytes==0 && cycles>0,"invalid GPU compute work");
      key="gpu_compute/card0";
    }
    else if(resource=="delay") {key="delay/"+std::to_string(tag);}
    else throw ConfigurationError("TokenStackFabric: unknown work resource {}",resource);
    check(resource=="delay" || resource=="gpu_compute" || (std::isfinite(bandwidth)&&bandwidth>0),"invalid work bandwidth");
    Clk_t length=std::max<Clk_t>(std::max<Clk_t>(1,cycles),bandwidth>0?transfer(bytes,bandwidth):1);
    if(tsv)length=std::max(length,transfer(bytes,m_tsv));
    // A streaming engine can pipeline pages; each conversion/layout stage uses
    // one execution lane. Descriptor/page lifetime is bounded by SRAM slots.
    if(resource=="convert" || resource=="transpose") {
      const int engines=m_hw["base_die"]["dma_engines"].as<int>();check(engines>0,"invalid DMA engine count");
      bool found=false;
      for(int e=0;e<engines;++e) {
        std::string candidate=key+(e?"/e"+std::to_string(e):"");
        if(available({candidate,m_clk,m_clk+length,bytes})) {key=candidate;found=true;break;}
      }
      if(!found)return false;
    }
    std::vector<Reservation> reservations={{key,m_clk,m_clk+length,bytes}};
    if(tsv) reservations.push_back({stack_key("tsv",stack),m_clk,m_clk+std::max(length,transfer(bytes,m_tsv)),bytes});
    if(!reserve(reservations,tag,source)) return false;
    m_events.push_back({m_clk+length,done});return true;
  }
  void tick() override {
    ++m_clk;dispatch();
    // Rotate die traversal: fixed first-die priority would starve later dies
    // sharing a command bus. Backend clocks/callbacks/refresh always tick.
    for(size_t n=0;n<m_layers.size();++n) m_layers[(n+size_t(m_clk))%m_layers.size()].backend->tick();
    std::vector<Event> ready;auto it=m_events.begin();
    while(it!=m_events.end()) {if(it->end<=m_clk){ready.push_back(*it);it=m_events.erase(it);}else ++it;}
    for(auto& event:ready)event.done(event.end);
    // Keep one old turnaround guard interval; completed history lives in CSV.
    for(auto& [key,calendar]:m_calendar) calendar.erase(std::remove_if(calendar.begin(),calendar.end(),[&](const auto& r){return r.end+m_turn<m_clk;}),calendar.end());
  }
  float get_tCK() override {return m_tck/1000.0f;}
  bool is_pending() override {
    if(!m_events.empty())return true;
    for(const auto& [key,queue]:m_queues)if(!queue.empty())return true;
    for(auto& layer:m_layers)if(layer.backend->is_pending())return true;
    return false;
  }
  void finalize() override {
    m_commands.flush();m_resources.flush();m_event_log.flush();
    YAML::Emitter emitter;emitter<<YAML::BeginMap;m_impl->print_stats(emitter);
    emitter<<YAML::Key<<"PhysicalDies"<<YAML::Value<<YAML::BeginSeq;
    for(auto& layer:m_layers) {
      std::ostringstream stream;auto* old=std::cout.rdbuf(stream.rdbuf());layer.backend->finalize();std::cout.rdbuf(old);
      emitter<<YAML::BeginMap<<YAML::Key<<"stack"<<YAML::Value<<layer.stack<<YAML::Key<<"die_id"<<YAML::Value<<layer.id<<YAML::Key<<"kind"<<YAML::Value<<layer.kind
             <<YAML::Key<<"bytes"<<YAML::Value<<layer.bytes<<YAML::Key<<"stats"<<YAML::Value<<YAML::Load(stream.str())["MemorySystem"]<<YAML::EndMap;
    }
    emitter<<YAML::EndSeq<<YAML::EndMap;std::cout<<emitter.c_str()<<std::endl;
  }
};
}
