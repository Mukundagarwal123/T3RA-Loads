"""Tests for the tracing helpers.

Tracing is decoration. It must never change what the wrapped code does, and in
particular must never swallow or replace an exception -- an earlier version
turned every real error into ``RuntimeError: generator didn't stop after
throw()``, which hid the actual cause behind pages of unrelated traceback.
"""

from __future__ import annotations

import pytest

from t3ra_unified.observability import logfire_span, traced_function


class Boom(Exception):
    pass


class TestLogfireSpan:
    def test_the_body_runs(self):
        ran = []
        with logfire_span("test"):
            ran.append(True)
        assert ran == [True]

    def test_an_exception_propagates_unchanged(self):
        with pytest.raises(Boom), logfire_span("test"):
            raise Boom("the real error")

    def test_the_original_exception_is_not_replaced(self):
        """The caller must see Boom, not a RuntimeError about generators."""
        with pytest.raises(Boom, match="the real error"), logfire_span("test"):
            raise Boom("the real error")

    def test_it_survives_logfire_being_unusable(self, monkeypatch):
        """A broken tracer must not break the code it is tracing."""
        import t3ra_unified.observability as obs

        def explode(*args, **kwargs):
            raise RuntimeError("logfire is broken")

        monkeypatch.setattr("logfire.span", explode)

        ran = []
        with obs.logfire_span("test"):
            ran.append(True)
        assert ran == [True]

        with pytest.raises(Boom), obs.logfire_span("test"):
            raise Boom("still propagates")


class TestTracedFunction:
    def test_the_return_value_passes_through(self):
        @traced_function()
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_an_exception_propagates_unchanged(self):
        @traced_function()
        def fails():
            raise Boom("the real error")

        with pytest.raises(Boom, match="the real error"):
            fails()

    def test_the_wrapped_function_keeps_its_identity(self):
        @traced_function()
        def documented():
            """Original docstring."""

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Original docstring."

    def test_async_functions_are_wrapped_too(self):
        import asyncio

        @traced_function()
        async def fails():
            raise Boom("async error")

        with pytest.raises(Boom, match="async error"):
            asyncio.run(fails())
