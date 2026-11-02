from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from .nodes import Nodes, SearchFn
from .state import GraphState


def build_graph(llm: BaseChatModel, search: SearchFn, output_dir: str = "output"):
    """plan_queries -> search (fan-out) -> extract -> dedupe -> verify -> reflect -> (loop | consolidate -> rank -> write_report)"""
    nodes = Nodes(llm, search, output_dir)
    g = StateGraph(GraphState)

    g.add_node("plan_queries", nodes.plan_queries)
    g.add_node("search", nodes.run_search)
    g.add_node("extract", nodes.extract)
    g.add_node("dedupe", nodes.dedupe)
    g.add_node("verify", nodes.verify)
    g.add_node("reflect", nodes.reflect)
    g.add_node("consolidate", nodes.consolidate)
    g.add_node("rank", nodes.rank)
    g.add_node("write_report", nodes.write_report)

    g.add_edge(START, "plan_queries")
    g.add_conditional_edges("plan_queries", nodes.fan_out_search, ["search", "extract"])
    g.add_edge("search", "extract")
    g.add_edge("extract", "dedupe")
    g.add_edge("dedupe", "verify")
    g.add_edge("verify", "reflect")
    g.add_conditional_edges("reflect", nodes.route_after_reflect, ["plan_queries", "consolidate"])
    g.add_edge("consolidate", "rank")
    g.add_edge("rank", "write_report")
    g.add_edge("write_report", END)

    return g.compile()
