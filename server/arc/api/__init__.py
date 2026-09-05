"""HTTP routers.

M0 shipped ``health``; M1 adds ``jobs``. M2+ adds auth, users, anime, list,
episodes, progress, media, mal, schedule, recs and admin (architecture.md §3).

Routers are registered in :func:`arc.main.create_app`.
"""
