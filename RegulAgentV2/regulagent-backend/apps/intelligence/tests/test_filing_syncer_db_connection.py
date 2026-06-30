"""
Regression tests for the FilingSyncer DB-connection-leak hotfix.

Background
----------
``filing_syncer.py`` used to run every ORM call via
``asyncio.get_event_loop().run_in_executor(None, lambda: <ORM>)``.  The default
executor is a multi-thread pool; Django connections are thread-local, so each
call opened a connection on an arbitrary pool thread that nothing ever closed
(Celery's Django fixup only closes the *main* task thread's connection at task
boundaries).  Under concurrency this leaked one connection per pool thread until
Postgres ``max_connections`` was exhausted and the app went down.

The fix routes all ORM through ``_run_db`` →
``sync_to_async(fn, thread_sensitive=True)`` with ``close_old_connections()`` in
a ``finally``.  ``thread_sensitive=True`` serialises ORM onto a single shared
thread, so the connection count is bounded to O(1) regardless of how many calls
run — instead of O(pool threads) under the old pattern.

These tests are written in the project's sync-driver style (no ``pytest-asyncio``
in this repo): the coroutine is driven via ``run_until_complete``.
"""

import ast
import asyncio
import os

import pytest

from django.db import connection

from apps.intelligence.services import filing_syncer as _fs_module
from apps.intelligence.services.filing_syncer import _run_db


def _db_conn_count() -> int:
    """Open backend connections to the current test database."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        return cur.fetchone()[0]


def _slow_query():
    """Hold a connection briefly so concurrent calls would occupy distinct
    pool threads under the OLD run_in_executor pattern."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_sleep(0.1)")
    return 1


@pytest.mark.django_db(transaction=True)
def test_run_db_does_not_leak_connections_under_concurrency():
    """
    Firing many ORM calls concurrently through ``_run_db`` must NOT grow the
    open-connection count by more than 1 (the single thread-sensitive worker
    thread).  The old ``run_in_executor(None, ...)`` pattern would leave one
    residual connection per pool thread (~N), so a regression to that pattern
    makes this assertion fail.
    """
    if "postgresql" not in connection.settings_dict["ENGINE"]:
        pytest.skip("connection-count assertion requires PostgreSQL")

    n_calls = 8

    async def batch():
        # thread_sensitive=True serialises these onto one shared thread → O(1)
        # connections.  run_in_executor(None, ...) would fan out to N threads.
        await asyncio.gather(*[_run_db(_slow_query) for _ in range(n_calls)])

    baseline = _db_conn_count()
    asyncio.get_event_loop().run_until_complete(batch())
    residual = _db_conn_count() - baseline

    assert residual <= 1, (
        f"_run_db leaked {residual} connections across {n_calls} concurrent "
        f"calls (expected <= 1); the run_in_executor leak may have regressed"
    )


def test_filing_syncer_uses_no_run_in_executor():
    """
    Structural guard: there must be no real ``*.run_in_executor(...)`` CALL in
    filing_syncer.py.  Parsed via AST so the docstring that *describes* the old
    pattern does not trigger a false positive (a plain regex would match it).
    """
    src_path = _fs_module.__file__
    with open(src_path, "r") as fh:
        tree = ast.parse(fh.read())

    offending = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_in_executor"
    ]
    assert not offending, (
        f"{os.path.basename(src_path)} still contains "
        f"{len(offending)} run_in_executor call(s) — use _run_db instead"
    )
