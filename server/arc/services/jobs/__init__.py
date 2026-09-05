"""Job table, claim/run/retry loop, and the handler registry.

M1 adds ``claim``/``run``/``retry`` over the ``jobs`` table using
``SELECT … FOR UPDATE SKIP LOCKED`` plus a registry mapping job type to an
idempotent, retry-safe handler (architecture.md §2, §5).
"""
