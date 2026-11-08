"""Drop-in mode: Pygments itself uses the native engine where it is exact.

``install()`` patches, for every lexer class rustgments runs natively:

* ``get_tokens_unprocessed``: native tokens for instances of exactly that
  class (subclasses, other start states and non-str input keep Pygments' own
  method).  Code that only drives lexers, such as ``DelegatingLexer`` or
  ``using(...)`` callbacks of other lexers, picks this up too.
* ``get_tokens``: Pygments' own preprocessing (``_preprocess_lexer_input``
  with the lexer's options), then a token stream that lexes natively when it
  is first read and runs the lexer's filters on it in Python.

and ``HtmlFormatter.format_unencoded``: when it receives such a stream
untouched, ``_format_lines`` (the per-token loop) runs natively with span
openers computed by the formatter's own code; every per-line step (line
numbers, ``hl_lines``, anchors, ``wrap``, ``<div>``, ``full``) still runs
the formatter's own methods.  A stream with a token of type ``Error`` and a
``raiseonerror`` filter takes Pygments' path, so the error is raised at the
same point with the same partial output.

Everything else (other lexers, other formatters, unknown filters, overridden
``_format_lines``, ``tagsfile``) runs Pygments untouched.  ``uninstall()``
restores the original methods.
"""

import threading

from . import _native, EngineFailure

_LOCK = threading.Lock()
_SAVED = []          # (owner, attribute, original value or _MISSING)
_MISSING = object()
_INSTALLED = False

_HANDLES = {}        # opener configuration key -> native handle

# How often each path ran (for tests and benchmarks; plain ints, no locking).
STATS = {'lex_native': 0, 'lex_pygments': 0, 'html_native': 0, 'html_pygments': 0}


def _save(owner, name, value):
    _SAVED.append((owner, name, owner.__dict__.get(name, _MISSING)))
    setattr(owner, name, value)


class NativeTokens:
    """The token stream of ``Lexer.get_tokens`` for a native lexer.

    It behaves like the generator Pygments returns: an iterator of
    ``(tokentype, value)`` pairs, lexed on first use, with the lexer's filters
    applied.  The HTML formatter recognises an untouched stream and formats it
    natively.
    """

    __slots__ = ('lexer', 'class_name', 'text', 'filters', '_it')

    def __init__(self, lexer, class_name, text, filters):
        self.lexer = lexer
        self.class_name = class_name
        self.text = text
        self.filters = filters
        self._it = None

    def __iter__(self):
        return self

    def __next__(self):
        if self._it is None:
            self._it = self._stream()
        return next(self._it)

    def close(self):
        self._it = iter(())

    def _stream(self):
        from pygments.filter import apply_filters
        toks = None
        if type(self.text) is str:
            try:
                toks = _native.tokens(self.class_name, self.text)
                STATS['lex_native'] += 1
            except (EngineFailure, UnicodeEncodeError):
                toks = None
        if toks is None:
            toks = ((t, v) for _, t, v in self.lexer.get_tokens_unprocessed(self.text))
        stream = iter(toks)
        if self.filters:
            stream = apply_filters(stream, self.filters, self.lexer)
        return stream


def _patch_lexer(cls, class_name):
    orig_gtu = cls.get_tokens_unprocessed
    orig_gt = cls.get_tokens
    from pygments.lexer import RegexLexer
    regex = issubclass(cls, RegexLexer)

    if regex:
        def get_tokens_unprocessed(self, text, stack=('root',)):
            if type(self) is cls and type(text) is str and stack == ('root',):
                try:
                    toks = _native.tokens_with_offsets(class_name, text)
                    STATS['lex_native'] += 1
                    return iter(toks)
                except (EngineFailure, UnicodeEncodeError):
                    pass
            STATS['lex_pygments'] += 1
            return orig_gtu(self, text, stack)
    else:
        def get_tokens_unprocessed(self, text):
            if type(self) is cls and type(text) is str:
                try:
                    toks = _native.tokens_with_offsets(class_name, text)
                    STATS['lex_native'] += 1
                    return iter(toks)
                except (EngineFailure, UnicodeEncodeError):
                    pass
            STATS['lex_pygments'] += 1
            return orig_gtu(self, text)

    def get_tokens(self, text, unfiltered=False):
        if type(self) is not cls:
            return orig_gt(self, text, unfiltered)
        # Lexer.get_tokens: preprocess eagerly, lex lazily, then filters
        text = self._preprocess_lexer_input(text)
        return NativeTokens(self, class_name, text, () if unfiltered else self.filters)

    get_tokens_unprocessed.__doc__ = orig_gtu.__doc__
    get_tokens.__doc__ = orig_gt.__doc__
    _save(cls, 'get_tokens_unprocessed', get_tokens_unprocessed)
    _save(cls, 'get_tokens', get_tokens)


def _opener_handle(fmt):
    """Native handle for the span openers and line separator of `fmt`.

    The openers come from the formatter's own `_format_lines` (run once over
    every token type), so `noclasses`, styles, `classprefix` and
    `debug_token_types` behave exactly as in Pygments.  A fresh formatter of a
    class that does not override the CSS helpers depends only on the key
    below, so its handle is shared; anything else is computed per instance.
    """
    from pygments.formatters.html import HtmlFormatter
    from . import _token_types
    cached = fmt.__dict__.get('_rustgments_handle')
    if cached is not None:
        return cached
    t = type(fmt)
    shareable = (not fmt.span_element_openers
                 and t._get_css_class is HtmlFormatter._get_css_class
                 and t._get_css_classes is HtmlFormatter._get_css_classes
                 and t._get_css_inline_styles is HtmlFormatter._get_css_inline_styles)
    key = None
    if shareable:
        key = (t, bool(fmt.noclasses), fmt.classprefix, bool(fmt.debug_token_types),
               fmt.lineseparator, fmt.style if fmt.noclasses else None)
        h = _HANDLES.get(key)
        if h is not None:
            return h
    tts = _token_types()
    for _ in fmt._format_lines([(tt, 'x') for tt in tts]):
        pass
    openers = [fmt.span_element_openers[tt] for tt in tts]
    h = _native.register_openers(openers, fmt.lineseparator)
    if key is not None:
        _HANDLES[key] = h
    else:
        fmt.__dict__['_rustgments_handle'] = h
    return h


def _patch_html_formatter():
    from pygments.filters import RaiseOnErrorTokenFilter
    from pygments.formatters.html import HtmlFormatter
    orig = HtmlFormatter.format_unencoded
    orig_lines = HtmlFormatter._format_lines
    orig_translate = HtmlFormatter._translate_parts

    def native_lines(self, ts):
        t = type(self)
        if (t._format_lines is not orig_lines or t._translate_parts is not orig_translate
                or self.tagsfile or type(ts.text) is not str):
            return None
        if any(type(f) is not RaiseOnErrorTokenFilter for f in ts.filters):
            return None
        try:
            return _native.format_lines(ts.class_name, ts.text, _opener_handle(self), bool(ts.filters))
        except (EngineFailure, UnicodeEncodeError):
            return None

    def format_unencoded(self, tokensource, outfile):
        if type(tokensource) is NativeTokens and tokensource._it is None:
            lines = native_lines(self, tokensource)
            if lines is not None:
                STATS['html_native'] += 1
                tokensource._it = iter(())
                source = ((1, line) for line in lines)
                # The rest is HtmlFormatter.format_unencoded (Pygments 2.21.0)
                # after `source = self._format_lines(tokensource)`.
                if not self.nowrap and self.linenos == 2:
                    source = self._wrap_inlinelinenos(source)
                if self.hl_lines:
                    source = self._highlight_lines(source)
                if not self.nowrap:
                    if self.lineanchors:
                        source = self._wrap_lineanchors(source)
                    if self.linespans:
                        source = self._wrap_linespans(source)
                    source = self.wrap(source)
                    if self.linenos == 1:
                        source = self._wrap_tablelinenos(source)
                    source = self._wrap_div(source)
                    if self.full:
                        source = self._wrap_full(source, outfile)
                for t, piece in source:
                    outfile.write(piece)
                return None
        STATS['html_pygments'] += 1
        return orig(self, tokensource, outfile)

    format_unencoded.__doc__ = orig.__doc__
    _save(HtmlFormatter, 'format_unencoded', format_unencoded)


# sha256 of the source of HtmlFormatter.format_unencoded in Pygments 2.21.0,
# whose tail is copied above; install() refuses to patch anything else.
_FORMAT_UNENCODED_SHA256 = '70460d4f03e958a4052d16e84b873aaa1b60d8d90af84c0ef2e81e93377d860a'


def _formatter_source_ok():
    import hashlib
    import inspect
    from pygments.formatters.html import HtmlFormatter
    try:
        src = inspect.getsource(HtmlFormatter.format_unencoded)
    except (OSError, TypeError):
        return True  # no source shipped; the Pygments version pin still holds
    return hashlib.sha256(src.encode()).hexdigest() == _FORMAT_UNENCODED_SHA256


def install():
    """Route Pygments' own lexers and HtmlFormatter through rustgments where
    the output is identical.  Returns True if installed, False when this
    interpreter / Pygments version has no native tables (Pygments then runs
    unchanged)."""
    global _INSTALLED
    from . import _VERSION_OK, _LEXERS, _NATIVE_BACKENDS, _token_types
    with _LOCK:
        if _INSTALLED:
            return True
        if not _VERSION_OK:
            return False
        import pygments.lexers
        if not _formatter_source_ok():
            return False
        _native.set_token_types(_token_types())
        for name, info in _LEXERS.items():
            if info[3] in _NATIVE_BACKENDS:
                _patch_lexer(getattr(pygments.lexers, name), name)
        _patch_html_formatter()
        _INSTALLED = True
        return True


def uninstall():
    """Restore every method install() replaced."""
    global _INSTALLED
    with _LOCK:
        while _SAVED:
            owner, name, value = _SAVED.pop()
            if value is _MISSING:
                delattr(owner, name)
            else:
                setattr(owner, name, value)
        _INSTALLED = False


def installed():
    return _INSTALLED
