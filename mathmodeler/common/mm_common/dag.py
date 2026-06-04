"""mm_common.dag — Kahn topological sort + linear fallback (tech-design §2.8).

Ported verbatim from the reference implementation ``agent/coordinator.py``:
  * ``compute_dag_order(graph)`` — Kahn's algorithm over a dependency
    adjacency map ``{node: [deps...]}``; raises ``ValueError`` on a cycle.
  * ``fallback_linear_dag(tasknum)`` — the original linear fallback used when
    DAG JSON parsing fails after retries: ``{i: [1..i-1]}``.
"""
from __future__ import annotations

from collections import deque


def compute_dag_order(graph: dict[str, list[str]]) -> list[str]:
    """Return an executable topological order of ``graph``.

    ``graph`` maps each node to the list of nodes it depends on. Raises
    ``ValueError("Graph contains a cycle!")`` if no valid ordering exists.
    """
    in_degree = {n: len(graph[n]) for n in graph}
    queue = deque([n for n in in_degree if in_degree[n] == 0])
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for m in graph:
            if n in graph[m]:
                in_degree[m] -= 1
                if in_degree[m] == 0:
                    queue.append(m)
    if len(order) != len(graph):
        raise ValueError("Graph contains a cycle!")
    return order


def fallback_linear_dag(tasknum: int) -> dict[str, list[str]]:
    """DAG-parse fallback (faithful to the original): ``{i: [1..i-1]}``."""
    return {str(i): [str(j) for j in range(1, i)] for i in range(1, tasknum + 1)}
