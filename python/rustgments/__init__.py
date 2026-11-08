"""rustgments: Pygments-compatible highlighting with a Rust engine.

The token tables are generated from Pygments 2.21.0 (BSD-2-Clause, see
``rustgments.PYGMENTS_NOTICE``).  Lexers that the Rust engine cannot reproduce
exactly fall back to Pygments itself; ``Lexer.backend`` says which one is used.

Pygments submodules are imported lazily: the native highlight path never
needs them.
"""

import pygments

from . import _native

__version__ = '0.1.0'

__all__ = ['get_lexer_by_name', 'highlight', 'HtmlFormatter', 'Lexer', 'backend_of',
           'EngineFailure', 'PYGMENTS_NOTICE', 'install', 'uninstall']

EngineFailure = _native.EngineFailure
# Native tables are only valid for the Pygments version they were built from,
# and for the interpreter they were built with: Python's \w, \d, \s and its
# case-insensitive matching follow the interpreter's Unicode database and
# `re` module.  The IR carries one table variant per supported Python
# version; pick ours before anything else touches the engine.
import sys as _sys
import unicodedata as _ud
_PYTHON_TAG = '%d.%d' % _sys.version_info[:2]
_VARIANT_OK = _native.select_variant(_PYTHON_TAG, _ud.unidata_version)
_INFO = _native.info()
PYGMENTS_NOTICE = _INFO['notice']
PYGMENTS_VERSION = _INFO['pygments_version']
_VERSION_OK = pygments.__version__ == PYGMENTS_VERSION and _VARIANT_OK

_LEXERS = {}
_ALIASES = {}
for _cls, _name, _aliases, _filenames, _backend, _reason in _native.lexers():
    _LEXERS[_cls] = (_name, tuple(_aliases), tuple(_filenames), _backend, _reason)
    for _a in _aliases:
        _ALIASES[_a] = _cls

_NATIVE_BACKENDS = ('native', 'handwritten')
_TOKENS = None


def _token_types():
    global _TOKENS
    if _TOKENS is None:
        from pygments.token import string_to_tokentype
        _TOKENS = [string_to_tokentype(n) for n in _native.token_names()]
    return _TOKENS


def _pyg_lexers():
    import pygments.lexers
    return pygments.lexers


class Lexer:
    """A lexer that runs natively when it can, else wraps the Pygments lexer."""

    def __init__(self, class_name, **options):
        self.class_name = class_name
        self.options = options
        self._pyg = None
        info = _LEXERS.get(class_name)
        if info is None:
            self.backend = 'fallback'
            self.reason = 'not in the rustgments native set'
        elif not _VERSION_OK:
            self.backend = 'fallback'
            self.reason = (f'no tables for Pygments {pygments.__version__} on Python {_PYTHON_TAG} '
                           f'(Unicode {_ud.unidata_version}); tables are for Pygments {PYGMENTS_VERSION}')
        elif options:
            self.backend = 'fallback'
            self.reason = f'non-default lexer options {sorted(options)}'
        elif info[3] in _NATIVE_BACKENDS:
            self.backend = 'native'
            self.reason = info[4] if info[3] == 'handwritten' else None
        else:
            self.backend = 'fallback'
            self.reason = info[4]
        self.name = info[0] if info else self.pygments_lexer.name

    def __repr__(self):
        return f'<rustgments.Lexer {self.class_name} backend={self.backend}>'

    @property
    def pygments_lexer(self):
        if self._pyg is None:
            self._pyg = getattr(_pyg_lexers(), self.class_name)(**self.options)
        return self._pyg

    def get_tokens_unprocessed(self, text):
        if self.backend == 'native' and isinstance(text, str):
            try:
                raw = _native.get_tokens_unprocessed(self.class_name, text)
            except (EngineFailure, UnicodeEncodeError):
                pass
            else:
                toks = _token_types()
                return [(i, toks[t], v) for i, t, v in raw]
        return list(self.pygments_lexer.get_tokens_unprocessed(text))

    def get_tokens(self, text):
        if self.backend == 'native' and isinstance(text, str):
            try:
                pre = _native.preprocess(text)
                raw = _native.get_tokens_unprocessed(self.class_name, pre)
            except (EngineFailure, UnicodeEncodeError):
                pass
            else:
                toks = _token_types()
                return [(toks[t], v) for _, t, v in raw]
        return list(self.pygments_lexer.get_tokens(text))


def get_lexer_by_name(alias, **options):
    cls = _ALIASES.get(alias)
    if cls is None:
        cls = type(_pyg_lexers().get_lexer_by_name(alias)).__name__
    return Lexer(cls, **options)


def backend_of(alias):
    return get_lexer_by_name(alias).backend


class HtmlFormatter:
    """HtmlFormatter; native only with default options."""

    def __init__(self, **options):
        self.options = options
        self.native = not options
        self._pyg = None

    @property
    def pygments_formatter(self):
        if self._pyg is None:
            from pygments.formatters.html import HtmlFormatter as PygHtmlFormatter
            self._pyg = PygHtmlFormatter(**self.options)
        return self._pyg

    def get_style_defs(self, arg=None):
        return self.pygments_formatter.get_style_defs(arg)

    def format(self, tokensource, outfile):
        return self.pygments_formatter.format(tokensource, outfile)


def _native_lexer_class(lexer):
    if isinstance(lexer, Lexer):
        return lexer.class_name if lexer.backend == 'native' else None
    # a plain Pygments lexer instance of a native class with default options
    name = type(lexer).__name__
    info = _LEXERS.get(name)
    if (info and _VERSION_OK and info[3] in _NATIVE_BACKENDS
            and type(lexer) is getattr(_pyg_lexers(), name, None)
            and not lexer.options and not lexer.filters):
        return name
    return None


def _native_formatter(formatter):
    if isinstance(formatter, HtmlFormatter):
        return formatter.native
    from pygments.formatters.html import HtmlFormatter as PygHtmlFormatter
    return type(formatter) is PygHtmlFormatter and not formatter.options


def highlight(code, lexer, formatter, outfile=None):
    """Like ``pygments.highlight``; native for native lexers + default HtmlFormatter."""
    cls = _native_lexer_class(lexer)
    if cls is not None and isinstance(code, str) and _native_formatter(formatter):
        try:
            html = _native.highlight_html(cls, code)
        except (EngineFailure, UnicodeEncodeError):
            html = None
        if html is not None:
            if outfile is not None:
                outfile.write(html)
                return None
            return html
    from pygments import highlight as pyg_highlight
    pyg_lexer = lexer.pygments_lexer if isinstance(lexer, Lexer) else lexer
    pyg_fmt = formatter.pygments_formatter if isinstance(formatter, HtmlFormatter) else formatter
    return pyg_highlight(code, pyg_lexer, pyg_fmt, outfile)


def install():
    """Drop-in mode: make Pygments' own ``highlight``, ``Lexer.get_tokens``,
    ``Lexer.get_tokens_unprocessed`` and ``HtmlFormatter`` use the native
    engine for native lexers wherever the output is identical (see
    ``rustgments._dropin``).  Returns False, changing nothing, when this
    interpreter or Pygments version has no native tables."""
    from ._dropin import install as _install
    return _install()


def uninstall():
    """Undo ``install()``."""
    from ._dropin import uninstall as _uninstall
    _uninstall()
