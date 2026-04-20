"""TokenTracker + TurnUsage."""

from types import SimpleNamespace

from agent_runtime.core.tracking import PRICING, TokenTracker, TurnUsage


def test_turn_usage_addition():
    a = TurnUsage(input_tokens=10, output_tokens=5)
    b = TurnUsage(input_tokens=20, output_tokens=15, cache_read_input_tokens=100)
    s = a + b
    assert s.input_tokens == 30
    assert s.output_tokens == 20
    assert s.cache_read_input_tokens == 100


def test_cost_uses_correct_rates():
    rates = PRICING["claude-sonnet-4-6"]
    t = TurnUsage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_read_input_tokens=1_000_000)
    expected = rates["input"] + rates["output"] + rates["cache_read"]
    assert abs(t.cost("claude-sonnet-4-6") - expected) < 1e-6


def test_cost_unknown_model_falls_back_to_sonnet():
    t = TurnUsage(input_tokens=1_000_000)
    rates = PRICING["claude-sonnet-4-6"]
    assert abs(t.cost("never-heard-of-this") - rates["input"]) < 1e-6


def test_tracker_record_and_total():
    tr = TokenTracker()
    usage1 = SimpleNamespace(input_tokens=100, output_tokens=50,
                             cache_creation_input_tokens=0, cache_read_input_tokens=0)
    usage2 = SimpleNamespace(input_tokens=200, output_tokens=80,
                             cache_creation_input_tokens=10, cache_read_input_tokens=20)
    tr.record(usage1)
    tr.record(usage2)
    assert tr.turn_count == 2
    assert tr.total.input_tokens == 300
    assert tr.total.output_tokens == 130
    assert tr.total.cache_read_input_tokens == 20


def test_tracker_handles_missing_attrs():
    tr = TokenTracker()
    # Some SDK responses omit cache fields entirely.
    minimal = SimpleNamespace(input_tokens=42, output_tokens=7)
    tr.record(minimal)
    assert tr.total.input_tokens == 42
    assert tr.total.cache_read_input_tokens == 0


def test_format_turn_includes_cost():
    tr = TokenTracker()
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    turn = tr.record(usage)
    s = tr.format_turn(turn, "claude-sonnet-4-6")
    assert "in=1000" in s
    assert "out=100" in s
    assert "$" in s


def test_reset_clears_all():
    tr = TokenTracker()
    tr.record(SimpleNamespace(input_tokens=1, output_tokens=1,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0))
    tr.reset()
    assert tr.turn_count == 0
    assert tr.total.input_tokens == 0
