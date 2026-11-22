#include <algorithm>
#include <fstream>
#include <map>
#include <set>
#include "frontend/frontend.h"
#include "memory_system/tokenstack_fabric.h"
namespace Ramulator {
// Trace DAG executor: timing is live, dependencies are completed callbacks, not
// precomputed offline latency sums. Numerical payload validation is separate.
class TokenStackGraph final : public IFrontEnd, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IFrontEnd, TokenStackGraph, "TokenStackGraph", "Versioned hardware task DAG.");
  struct Op { int type; Addr_t address; int id; uint32_t mask; };
  struct Task {
    std::string id, source, resource;
    std::string acquire,release;
    std::string page_lock;
    int group=0;
    size_t buffer_bytes=0;
    std::vector<std::string> dependencies;
    std::vector<Op> operations;
    int stack=0, state=0;
    Clk_t issue=0, eligible=-1, start=-1, end=-1, cycles=0, served=-1;
    size_t bytes=0, sent=0, returned=0;
  };
  std::vector<Task> m_tasks;
  std::map<std::string,size_t> m_index;
  ITokenStackFabric* m_fabric=nullptr;
  Clk_t m_timeout=0;
  size_t m_done=0;
  std::ofstream m_log, m_requests,m_buffers;
  size_t m_group_capacity=0,m_buffer_peak=0;
  int m_groups=0;
  Clk_t m_aging=0;
  std::map<int,size_t> m_used;
  std::map<std::string,std::pair<int,size_t>> m_allocations;
  std::map<std::string,std::string> m_locks,m_allocation_locks;
  static void check(bool ok,const std::string& msg) { if(!ok) throw ConfigurationError("TokenStackGraph: {}",msg); }
  void complete(size_t index, Clk_t end) {
    auto& task=m_tasks[index]; check(task.state==1,"duplicate task completion");
    if(!task.release.empty()) {
      check(m_allocations.count(task.release),"release without allocation");
      const auto allocation=m_allocations.at(task.release);
      check(m_used[allocation.first]>=allocation.second,"buffer underflow");
      m_used[allocation.first]-=allocation.second;
      m_buffers<<"1,"<<task.release<<','<<allocation.first<<','<<end<<",release,"<<allocation.second<<','<<m_used[allocation.first]<<'\n';
      m_allocations.erase(task.release);
      if(m_allocation_locks.count(task.release)) {
        m_locks.erase(m_allocation_locks.at(task.release));m_allocation_locks.erase(task.release);
      }
    }
    task.state=2; task.end=end; ++m_done;
    m_log<<"1,"<<index<<','<<task.id<<','<<task.source<<','<<task.issue<<','<<task.eligible<<','<<task.start<<','<<end
         <<','<<task.bytes<<','<<task.operations.size()<<'\n';
  }
 public:
  void init() override {
    m_clock_ratio=param<uint>("clock_ratio").required(); check(m_clock_ratio==1,"requires 1:1 clock");
    m_timeout=param<Clk_t>("max_cycles").required(); check(m_timeout>0,"invalid timeout");
    m_group_capacity=param<size_t>("buffer_bytes_per_group").required();m_groups=param<int>("head_groups").required();
    m_aging=param<Clk_t>("aging_cycles").required();
    check(m_group_capacity>0 && m_groups>0 && m_aging>0,"invalid buffer/groups/fairness");
    auto input=YAML::LoadFile(param<std::string>("path").required());
    check(input["schema_version"].as<int>()==1 && input["tasks"].IsSequence(),"invalid graph schema");
    int request_id=0;
    for(auto node:input["tasks"]) {
      Task task; task.id=node["id"].as<std::string>(); task.source=node["source"].as<std::string>();
      check(task.source=="gpu"||task.source=="pim"||task.source=="promotion"||task.source=="demotion","invalid task source");
      check(!task.id.empty() && task.id.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")==std::string::npos
            && !m_index.count(task.id),"invalid/duplicate task ID");
      task.issue=node["issue_cycle"] ? node["issue_cycle"].as<Clk_t>() : 0;
      check(task.issue>=0,"negative issue cycle");
      if(node["depends_on"]) task.dependencies=node["depends_on"].as<std::vector<std::string>>();
      if(node["acquire"]) {
        task.acquire=node["acquire"].as<std::string>();task.group=node["buffer_group"].as<int>();task.buffer_bytes=node["buffer_bytes"].as<size_t>();
        check(task.group>=0 && task.group<m_groups && task.buffer_bytes>0 && task.buffer_bytes<=m_group_capacity,"invalid/oversized page buffer allocation");
        if(node["page_lock"]) task.page_lock=node["page_lock"].as<std::string>();
      }
      if(node["release"]) task.release=node["release"].as<std::string>();
      if(node["requests"]) for(auto req:node["requests"]) {
        uint64_t mask=req["byte_mask"]?req["byte_mask"].as<uint64_t>():0xffffffff;
        check(mask>0 && mask<=0xffffffff,"invalid burst byte mask");
        task.operations.push_back({req["type"].as<int>(),req["address"].as<Addr_t>(),request_id++,uint32_t(mask)});
      }
      if(node["resource"]) {
        check(task.operations.empty(),"task cannot mix work and memory");
        task.resource=node["resource"].as<std::string>(); task.stack=node["stack"].as<int>();
        task.bytes=node["bytes"].as<size_t>(); task.cycles=node["cycles"].as<Clk_t>();
      }
      check(task.acquire.empty() || (task.operations.empty() && task.resource.empty()),"allocation must be its own graph node");
      m_index[task.id]=m_tasks.size(); m_tasks.push_back(task);
    }
    check(!m_tasks.empty(),"empty graph");
    std::set<std::string> reachable;
    for(size_t pass=0;pass<m_tasks.size();++pass) for(const auto& task:m_tasks) {
      for(const auto& dep:task.dependencies) check(m_index.count(dep),"missing dependency");
      if(std::all_of(task.dependencies.begin(),task.dependencies.end(),[&](auto dep){return reachable.count(dep);})) reachable.insert(task.id);
    }
    check(reachable.size()==m_tasks.size(),"dependency cycle");
    m_log.open(param<std::string>("task_log").required()); m_requests.open(param<std::string>("request_log").required());
    m_buffers.open(param<std::string>("buffer_log").required());
    check(m_log.is_open() && m_requests.is_open() && m_buffers.is_open(),"cannot open logs");
    m_buffers<<"schema_version,page_id,head_group,cycle,event,bytes,occupied_bytes\n";
    m_log<<"schema_version,task_index,task_id,source,issue_cycle,eligible_cycle,start_cycle,complete_cycle,bytes,requests\n";
    m_requests<<"schema_version,request_id,task_index,type,address,accept_cycle,complete_cycle,byte_mask\n";
    register_stat(m_buffer_peak).name("page_buffer_peak_bytes_per_group");
  }
  void setup(IFrontEnd*, IMemorySystem* memory) override {
    m_fabric=dynamic_cast<ITokenStackFabric*>(memory); check(m_fabric,"requires TokenStackFabric");
  }
  void tick() override {
    check(m_clk<m_timeout,"simulation timeout");
    std::vector<size_t> order;
    for(size_t i=0;i<m_tasks.size();++i) {
      auto& task=m_tasks[i];if(task.state==2 || task.issue>m_clk)continue;
      if(!std::all_of(task.dependencies.begin(),task.dependencies.end(),[&](auto dep){return m_tasks[m_index.at(dep)].state==2;}))continue;
      if(task.eligible<0)task.eligible=m_clk;
      order.push_back(i);
    }
    auto priority=[](const std::string& source){return source=="gpu"||source=="pim"?0:source=="promotion"?1:2;};
    std::stable_sort(order.begin(),order.end(),[&](size_t a,size_t b){
      const auto& x=m_tasks[a];const auto& y=m_tasks[b];bool xa=m_clk-x.eligible>=m_aging,ya=m_clk-y.eligible>=m_aging;
      // Equal-age tasks rotate by last successful admission. Fixed index order
      // lets early channels refill a bounded source queue and starve the rest.
      return std::make_tuple(!xa,xa?0:priority(x.source),x.eligible,x.served,a)<std::make_tuple(!ya,ya?0:priority(y.source),y.eligible,y.served,b);
    });
    for(size_t i:order) {
      auto& task=m_tasks[i]; if(task.state==2 || task.issue>m_clk) continue;
      bool ready=std::all_of(task.dependencies.begin(),task.dependencies.end(),[&](auto dep){return m_tasks[m_index.at(dep)].state==2;});
      if(!ready) continue;
      if(task.eligible<0) task.eligible=m_clk;
      if(task.state==0 && !task.acquire.empty()) {
        if(m_used[task.group]+task.buffer_bytes>m_group_capacity)continue;
        if(!task.page_lock.empty() && m_locks.count(task.page_lock))continue;
        check(!m_allocations.count(task.acquire),"page allocated twice");
        if(!task.page_lock.empty()) {m_locks[task.page_lock]=task.acquire;m_allocation_locks[task.acquire]=task.page_lock;}
        m_allocations[task.acquire]={task.group,task.buffer_bytes};m_used[task.group]+=task.buffer_bytes;
        m_buffer_peak=std::max(m_buffer_peak,m_used[task.group]);
        m_buffers<<"1,"<<task.acquire<<','<<task.group<<','<<m_clk<<",allocate,"<<task.buffer_bytes<<','<<m_used[task.group]<<'\n';
      }
      if(!task.resource.empty()) {
        if(task.state==1) continue;
        if(m_fabric->work(int(i),task.stack,task.resource,task.bytes,task.cycles,task.source,[this,i](Clk_t end){complete(i,end);})) {
          task.state=1; task.start=m_clk;task.served=m_clk;
        }
      } else {
        if(task.state==0) {task.state=1;task.start=m_clk;}
        if(task.operations.empty()) {complete(i,m_clk);continue;}
        if(task.sent<task.operations.size()) {
          auto op=task.operations[task.sent]; const auto accept=m_clk;
          Request req(op.address,op.type); req.source_id=op.id;
          req.callback=[this,i,op,accept](Request& done){
            auto& owner=m_tasks[i];
            m_requests<<"1,"<<op.id<<','<<i<<','<<op.type<<','<<op.address<<','<<accept<<','<<done.depart<<','<<op.mask<<'\n';
            if(++owner.returned==owner.operations.size()) complete(i,done.depart);
          };
          if(m_fabric->submit(req,task.source)) {++task.sent;task.served=m_clk;}
        }
      }
    }
    ++m_clk;
  }
  bool is_finished() override {return m_done==m_tasks.size();}
  void finalize() override {check(is_finished() && m_allocations.empty(),"unfinished graph or leaked page buffer");m_log.flush();m_requests.flush();m_buffers.flush();IFrontEnd::finalize();}
};
}
