"""``SERVE_NL2SQL_EXEC_TIMEOUT_S`` — operator override for the per-statement
execution timeout on the no-deadline (AgentCore) path.

Default stays the historical 35s so no deployment moves. The override exists for
federated sources where metadata rediscovery alone costs 20-40s per statement.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.tier2.nl_to_sql import strategy as strategy_mod
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy


def _strategy() -> NLtoSQLStrategy:
    return NLtoSQLStrategy(sql_generator=MagicMock(), firewall=MagicMock(), query_executor=AsyncMock())


def _ctx(remaining_s: float | None):
    deadline = None if remaining_s is None else SimpleNamespace(remaining_s=lambda: remaining_s)
    return SimpleNamespace(deadline=deadline)


class TestExecTimeoutOverride:
    def test_default_is_35_without_env(self, monkeypatch):
        monkeypatch.delenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", raising=False)
        assert strategy_mod._resolve_default_exec_timeout_s() == 35
        assert _strategy()._exec_timeout_seconds(_ctx(None)) == 35

    def test_override_applies_on_no_deadline_path(self, monkeypatch):
        monkeypatch.setenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", "90")
        assert _strategy()._exec_timeout_seconds(_ctx(None)) == 90

    @pytest.mark.parametrize(("raw", "expected"), [("1", 5), ("100000", 300), ("abc", 35), ("", 35), ("  ", 35)])
    def test_override_is_clamped_or_ignored(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", raw)
        assert strategy_mod._resolve_default_exec_timeout_s() == expected

    def test_deadline_still_caps_below_override(self, monkeypatch):
        """A threaded request deadline wins over the override: remaining - margin."""
        monkeypatch.setenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", "90")
        s = _strategy()
        assert s._exec_timeout_seconds(_ctx(remaining_s=20.0)) == 18  # 20 - 2s margin
        assert s._exec_timeout_seconds(_ctx(remaining_s=500.0)) == 90  # never above default

    def test_override_read_at_construction(self, monkeypatch):
        """Env is read when the strategy is built, not at import — matches max_shots."""
        monkeypatch.setenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", "60")
        built_with_60 = _strategy()
        monkeypatch.setenv("SERVE_NL2SQL_EXEC_TIMEOUT_S", "120")
        assert built_with_60._exec_timeout_seconds(_ctx(None)) == 60
        assert _strategy()._exec_timeout_seconds(_ctx(None)) == 120
