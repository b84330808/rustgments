"""Sphinx extension: ``extensions = [..., 'rustgments.sphinxext']``.

Calls ``rustgments.install()`` so that Sphinx's Pygments calls use the native
engine where the output is identical.  Output files do not change.
"""

import rustgments


def setup(app):
    rustgments.install()
    return {'version': '0.1.0', 'parallel_read_safe': True, 'parallel_write_safe': True}
