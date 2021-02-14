# Copyright 2020-2021 Robert Schroll
# This file is part of RMfuse and is distributed under the MIT license.

import functools

import trio

try:
    import pyfuse3 as fuse
    is_pyfuse3 = True

    def async_op(afunc):
        return afunc

except ImportError:
    try:
        import llfuse as fuse
        is_pyfuse3 = False

        def async_op(afunc):
            @functools.wraps(afunc)
            def decorated(*args, **kw):
                async def runner():
                    return await afunc(*args, **kw)
                return trio.run(runner)
            return decorated

        # llfuse doesn't have a FileInfo object.  llfuse just expects a file
        # handle to be returned in those places FileInfo is used in pyfuse3.
        fuse.FileInfo = lambda fh, **kw: fh

    except ImportError:
        raise RuntimeError('Need pyfuse3 or llfuse installed')
