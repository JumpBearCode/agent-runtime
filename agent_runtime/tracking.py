"""Token usage tracking — accumulates across API calls within a session."""

from dataclasses import dataclass, field


# Anthropic pricing per 1M tokens (USD)
PRICING = {
    "claude-opus-4-6":              {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":            {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001":    {"input": 0.80, "output": 4.0,  "cache_write": 1.0,   "cache_read": 0.08},
}


@dataclass
class TurnUsage:
    """Token usage for a single API call."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TurnUsage") -> "TurnUsage":
        return TurnUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    def cost(self, model: str) -> float:
        """Estimate cost in USD."""
        rates = PRICING.get(model)
        if not rates:
            # Fallback: use Sonnet pricing
            rates = PRICING["claude-sonnet-4-6"]
        # cache_read and cache_creation are subsets of input_tokens
        # Actual billed input = input_tokens - cache_read - cache_creation (at base rate)
        #                     + cache_creation (at write rate)
        #                     + cache_read (at read rate)
        base_input = self.input_tokens - self.cache_read_input_tokens - self.cache_creation_input_tokens
        return (
            base_input * rates["input"] / 1_000_000
            + self.cache_creation_input_tokens * rates["cache_write"] / 1_000_000
            + self.cache_read_input_tokens * rates["cache_read"] / 1_000_000
            + self.output_tokens * rates["output"] / 1_000_000
        )


@dataclass
class TokenTracker:
    """Accumulates token usage across multiple API calls."""
    _turns: list[TurnUsage] = field(default_factory=list)
    _total: TurnUsage = field(default_factory=TurnUsage)

    def record(self, usage) -> TurnUsage:
        """Record usage from an Anthropic API response.

        Args:
            usage: response.usage from Anthropic SDK (has input_tokens, output_tokens, etc.)

        Returns:
            TurnUsage for this call.
        """
        turn = TurnUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        self._turns.append(turn)
        self._total = self._total + turn
        return turn

    @property
    def total(self) -> TurnUsage:
        return self._total

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def format_turn(self, turn: TurnUsage, model: str) -> str:
        """Format a single turn's usage for display."""
        cost = turn.cost(model)
        parts = [f"in={turn.input_tokens}", f"out={turn.output_tokens}"]
        if turn.cache_read_input_tokens:
            parts.append(f"cache_read={turn.cache_read_input_tokens}")
        if turn.cache_creation_input_tokens:
            parts.append(f"cache_write={turn.cache_creation_input_tokens}")
        parts.append(f"${cost:.4f}")
        return " | ".join(parts)

    def format_total(self, model: str) -> str:
        """Format total usage summary."""
        t = self._total
        cost = t.cost(model)
        return (
            f"Tokens: {t.input_tokens:,} in + {t.output_tokens:,} out = {t.total_tokens:,} total"
            f" | Cache: {t.cache_read_input_tokens:,} read, {t.cache_creation_input_tokens:,} write"
            f" | Cost: ${cost:.4f}"
            f" | Turns: {self.turn_count}"
        )

    def reset(self):
        self._turns.clear()
        self._total = TurnUsage()
