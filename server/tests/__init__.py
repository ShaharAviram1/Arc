"""Test package.

The ``__init__.py`` is deliberate: it makes ``tests`` importable, so helpers
defined next to the fixtures (``tests.conftest.alembic_config``) can be
imported by name instead of relying on pytest's sys.path juggling.
"""
