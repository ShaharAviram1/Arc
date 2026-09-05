"""HTTP routers.

M0 shipped ``health``; M1 added ``jobs``; M2 adds ``auth``, ``invites`` and
``users``, plus the ``csrf`` origin check that guards every state-changing
call. M3+ adds anime, list, episodes, progress, media, mal, schedule, recs and
admin (architecture.md §3).

Routers are registered in :func:`arc.main.create_app`.

Access rules (spec §7). Public: ``/api/health``, ``/api/auth/login``,
``/api/auth/logout``, and the two token routes of ``/api/invites``. Everything
else needs a session, and ``/api/jobs``, ``/api/users`` and the three
admin routes of ``/api/invites`` additionally need the ``admin`` role.
"""
