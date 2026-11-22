add_library(ramulator-frontend OBJECT)

target_sources(
  ramulator-frontend PRIVATE
  frontend.h

  impl/memory_trace/loadstore_trace.cpp
  impl/memory_trace/pim_loadstore_trace.cpp
  impl/memory_trace/tokenstack_trace.cpp
  impl/memory_trace/tokenstack_graph.cpp
)

target_link_libraries(
  ramulator
  PRIVATE
  ramulator-frontend
)
