"""§10.1 dag.py — Kahn topological order, cycle detection, linear fallback."""
import pytest

from mm_common.dag import compute_dag_order, fallback_linear_dag


def test_linear_chain():
    graph = {"1": [], "2": ["1"], "3": ["2"]}
    assert compute_dag_order(graph) == ["1", "2", "3"]


def test_fork_join():
    # 1 -> {2,3} -> 4
    graph = {"1": [], "2": ["1"], "3": ["1"], "4": ["2", "3"]}
    order = compute_dag_order(graph)
    assert order[0] == "1"
    assert order[-1] == "4"
    assert order.index("2") < order.index("4")
    assert order.index("3") < order.index("4")
    assert set(order) == {"1", "2", "3", "4"}


def test_cycle_raises():
    graph = {"1": ["2"], "2": ["1"]}
    with pytest.raises(ValueError, match="cycle"):
        compute_dag_order(graph)


def test_fallback_linear_shape():
    dag = fallback_linear_dag(4)
    assert dag == {"1": [], "2": ["1"], "3": ["1", "2"], "4": ["1", "2", "3"]}
    # fallback is always a valid DAG -> ordered 1..n
    assert compute_dag_order(dag) == ["1", "2", "3", "4"]
