"""MkDocs plugin (``plugins: [rustgments]``): calls ``rustgments.install()``.

Pygments calls made by Markdown extensions (codehilite, pymdownx.highlight)
then use the native engine where the output is identical.
"""

from mkdocs.plugins import BasePlugin

import rustgments


class RustgmentsPlugin(BasePlugin):
    def on_config(self, config):
        rustgments.install()
        return config
