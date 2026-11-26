"""An LLM agent that drives Ulysses only through its MCP tools.

``run.py`` is the entry point. The model sits behind one narrow interface
(``models.ModelClient``): the real client calls Claude through the official
Anthropic SDK, and a scripted mock stands in when no ANTHROPIC_API_KEY is set,
so the scenarios and the tests run with no key and no network.
"""
