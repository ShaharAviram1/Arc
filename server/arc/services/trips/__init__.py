"""Trips: the next X aired episodes of one show, kept on a device (FR-A12, §5.4e).

Deliberately empty of imports. The leaf modules here (:mod:`.names`,
:mod:`.rules`, :mod:`.phase`) are imported by the reconciler, the linker, the
transcode sweep and the copy hook, all of which sit *underneath* the modules
that create and cancel trips; a package ``__init__`` that pulled those in would
close an import circle through acquisition.
"""
