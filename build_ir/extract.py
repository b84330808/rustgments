"""Build-time extractor: dump Pygments' processed RegexLexer tables to a JSON IR.

The script imports the pinned Pygments, instantiates each lexer so that
RegexLexerMeta processes the token definitions (include, inherit, combined,
default, words), and serializes ``cls._tokens``.  Every regex is re-emitted
from Python's own parse tree (``re._parser``) into Rust regex syntax, so the
Python-specific semantics (``\\w``/``\\d``/``\\s`` classes, ``\\Z``, ``$``,
flags) are spelled out explicitly instead of being trusted to line up.

Anything the IR cannot represent exactly marks the whole lexer as
"fallback" with a reason.  There is no partial translation.

Usage:  python -I build_ir/extract.py  (writes ir/pygments_ir.json)
"""

import hashlib
import inspect
import json
import re
import sys
from pathlib import Path

try:
    import re._constants as C
    import re._parser as P
except ImportError:  # Python 3.9 and 3.10
    import sre_constants as C
    import sre_parse as P

import pygments
from pygments.formatters.html import HtmlFormatter
from pygments.lexer import Lexer, RegexLexer, ExtendedRegexLexer
from pygments.token import _TokenType, STANDARD_TYPES
import pygments.lexers

IR_VERSION = 3

# Opcodes added in Python 3.11; a sentinel that matches nothing on 3.9/3.10.
_ABSENT = object()
POSSESSIVE_REPEAT = getattr(C, 'POSSESSIVE_REPEAT', _ABSENT)
ATOMIC_GROUP = getattr(C, 'ATOMIC_GROUP', _ABSENT)
PYGMENTS_PIN = '2.21.0'

# Core lexers (Python, JSON, JavaScript, Bash, Rust): native whatever the gate file says (they have
# their own, larger parity suite in tests/).
CORE = ['PythonLexer', 'JsonLexer', 'JavascriptLexer', 'BashLexer', 'RustLexer', 'TextLexer']

# Result of tests/gate.py: for every Pygments lexer, "pass" or the reason it
# stays on Pygments.  Only lexers that pass are extracted.
GATE_FILE = Path(__file__).resolve().parent / 'native_gate.json'

# JsonLexer and TextLexer are hand-written Lexers, not RegexLexers.
# rustgments carries hand ports of them; the hash pins the exact upstream
# source each port follows.
HANDWRITTEN = {
    'JsonLexer': 'json',
    'TextLexer': 'text',
}

def pygments_notice():
    """The BSD-2-Clause LICENSE text shipped in the installed Pygments wheel."""
    from importlib.metadata import distribution
    dist = distribution('pygments')
    for f in dist.files or ():
        if f.name == 'LICENSE':
            text = f.read_text(encoding='utf-8')
            head = f'Tables generated from Pygments {PYGMENTS_PIN} (https://pygments.org/).'
            return head + '\n\n' + text
    raise SystemExit('Pygments LICENSE not found in the installed distribution')


MAXCP = 0x10FFFF
SURR = (0xD800, 0xDFFF)


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------------------
# code point sets

def to_ranges(cps):
    out = []
    for cp in cps:
        if out and out[-1][1] + 1 == cp:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return [tuple(r) for r in out]


def normalize(ranges):
    """Sort, merge, and drop surrogates (they never occur in valid text)."""
    rs = []
    for lo, hi in ranges:
        if hi < SURR[0] or lo > SURR[1]:
            rs.append((lo, hi))
        else:
            if lo < SURR[0]:
                rs.append((lo, SURR[0] - 1))
            if hi > SURR[1]:
                rs.append((SURR[1] + 1, hi))
    rs.sort()
    out = []
    for lo, hi in rs:
        if out and lo <= out[-1][1] + 1:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def complement(ranges):
    ranges = normalize(ranges)
    out = []
    nxt = 0
    for lo, hi in ranges:
        if lo > nxt:
            out.append((nxt, lo - 1))
        nxt = hi + 1
    if nxt <= MAXCP:
        out.append((nxt, MAXCP))
    return normalize(out)


def compute_categories():
    """Exact code point sets of Python's \\d, \\s, \\w (str patterns)."""
    d, s, w = [], [], []
    rd, rs, rw = re.compile(r'\d').match, re.compile(r'\s').match, re.compile(r'\w').match
    for cp in range(MAXCP + 1):
        if SURR[0] <= cp <= SURR[1]:
            continue
        ch = chr(cp)
        if rd(ch):
            d.append(cp)
        if rs(ch):
            s.append(cp)
        if rw(ch):
            w.append(cp)
    return {'digit': to_ranges(d), 'space': to_ranges(s), 'word': to_ranges(w)}


ASCII_CATS = {
    'digit': [(0x30, 0x39)],
    'space': [(0x09, 0x0D), (0x20, 0x20)],
    'word': [(0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)],
}

CATS = None  # filled in main()


def category_ranges(cat, flags):
    table = ASCII_CATS if flags & re.ASCII else CATS
    m = {
        C.CATEGORY_DIGIT: ('digit', False), C.CATEGORY_NOT_DIGIT: ('digit', True),
        C.CATEGORY_SPACE: ('space', False), C.CATEGORY_NOT_SPACE: ('space', True),
        C.CATEGORY_WORD: ('word', False), C.CATEGORY_NOT_WORD: ('word', True),
    }
    if cat not in m:
        raise Unsupported(f'category {cat}')
    name, neg = m[cat]
    r = table[name]
    return complement(r) if neg else list(r)


# ---------------------------------------------------------------------------
# regex emission

def esc(cp):
    if cp < 128 and chr(cp).isalnum():
        return chr(cp)
    return '\\x{%X}' % cp


def class_body(ranges):
    return ''.join(esc(lo) if lo == hi else esc(lo) + '-' + esc(hi) for lo, hi in ranges)


# Class table.  Patterns refer to entries as @N@.  Emitted patterns escape
# every non-alphanumeric ASCII character, so a raw '@' never occurs in them and
# the placeholder is unambiguous.
#
# Two kinds of entries:
#   fixed     a large class spelled out by the lexer itself (explicit ranges);
#             the same on every Python version.
#   symbolic  a class whose members come from the interpreter's Unicode tables
#             or case rules: anything built from \d \s \w (and negations), and
#             every case-insensitive literal or set.  It is keyed by a
#             one-character Python pattern plus flags, and its members are
#             computed by this interpreter's own `re`.  Symbolic entries are
#             always placeholders, however short, so that pattern text is the
#             same on every Python version and only the table differs.
#
# Each entry also carries an ASCII-restricted twin (the class intersected with
# U+0000..U+007F).  The engine only uses the twin in a lazy DFA that quits on
# every non-ASCII byte, where both classes agree on every byte the search can
# examine.
CLASSES = []        # {'text', 'ascii'} plus 'key' for symbolic entries
FIXED_INDEX = {}    # text -> index
SYM_INDEX = {}      # (python pattern, flags) -> index
KEY_FLAGS = re.ASCII | re.IGNORECASE


def _texts(ranges):
    ranges = normalize(ranges)
    ascii_ranges = [(lo, min(hi, 0x7F)) for lo, hi in ranges if lo <= 0x7F]
    return _emit_set(ranges), _emit_set(ascii_ranges)


def emit_set(ranges):
    """A class whose members do not depend on the Python version."""
    text, ascii_text = _texts(ranges)
    if len(text) < 120:
        return text
    if text not in FIXED_INDEX:
        FIXED_INDEX[text] = len(CLASSES)
        CLASSES.append({'text': text, 'ascii': ascii_text})
    return '@%d@' % FIXED_INDEX[text]


def sym_class(py_pattern, flags, members):
    """A class computed from this interpreter: `py_pattern` is a one-character
    Python pattern, `members()` its code point ranges under `flags`."""
    key = (py_pattern, int(flags & KEY_FLAGS))
    if key not in SYM_INDEX:
        text, ascii_text = _texts(members())
        SYM_INDEX[key] = len(CLASSES)
        CLASSES.append({'key': list(key), 'text': text, 'ascii': ascii_text})
    return '@%d@' % SYM_INDEX[key]


def _emit_set(ranges):
    """Emit a flattened set; pick the shorter of [R] and [^complement]."""
    ranges = normalize(ranges)
    if not ranges:
        return '[^\\x{0}-\\x{10FFFF}]'
    comp = complement(ranges)
    if not comp:
        return '(?s:.)'
    if len(comp) < len(ranges):
        return '[^' + class_body(comp) + ']'
    if len(ranges) == 1 and ranges[0][0] == ranges[0][1]:
        return esc(ranges[0][0])
    return '[' + class_body(ranges) + ']'


class Emitter:
    """Turn an sre parse tree into a Rust regex / fancy-regex pattern.

    mode 'n': \\b and \\B use the regex crate's Unicode word boundary.
    mode 'd': \\b and \\B are spelled with lookarounds over Python's exact \\w set.
    mode 'pre': superset pattern with no lookaround/backreference, used only as
                a cheap "can this rule possibly match here?" filter.
    mode 'x':   exact rewrite without look-around for the regex crate:
                - a positive lookahead in trailing position becomes a capture
                  group; the Python match ends where that group starts (the
                  leftmost-first path of A(X) is the backtracking path of A(?=X));
                - a leading one-character lookbehind becomes a precondition on
                  the character before the match position.
                Capture groups are renumbered; `gmap` maps Python group numbers.
    """

    def __init__(self, mode):
        self.mode = mode
        self.fancy = False
        self.has_boundary = False
        self.next_group = 1
        self.gmap = {}
        self.la_groups = []
        self.lookbehind = None
        self.capture_depth = 0

    def word_class(self, flags):
        # \b follows plain \w membership; IGNORECASE does not change it.
        fl = flags & re.ASCII
        return sym_class(r'\w', fl, lambda: category_ranges(C.CATEGORY_WORD, fl))

    def seq(self, items, flags, trailing=False, top=False):
        out = []
        items = list(items)
        i = 0
        if self.mode == 'x' and top and items:
            op, av = items[0]
            if op is C.ASSERT_NOT or op is C.ASSERT:
                direction, p = av
                one = self.single_char_set(p, flags) if direction == -1 else None
                if one is not None:
                    self.lookbehind = {'neg': op is C.ASSERT_NOT, 'set': one}
                    i = 1
        while i < len(items):
            op, av = items[i]
            last = trailing and i == len(items) - 1
            if op is C.LITERAL:
                j = i
                s = ''
                while j < len(items) and items[j][0] is C.LITERAL:
                    cp = items[j][1]
                    if SURR[0] <= cp <= SURR[1]:
                        s += '[^\\x{0}-\\x{10FFFF}]'
                    else:
                        s += esc(cp)
                    j += 1
                if flags & re.IGNORECASE:
                    # Spell out Python's own case-insensitive match set per char.
                    s = ''.join(icase_class(py_literal(items[k][1]), flags)
                                for k in range(i, j))
                out.append(s)
                i = j
                continue
            out.append(self.item(op, av, flags, last))
            i += 1
        return ''.join(out)

    def item(self, op, av, flags, trailing=False):
        if op is C.NOT_LITERAL:
            if flags & re.IGNORECASE:
                return icase_class('[^' + py_literal(av) + ']', flags)
            return emit_set(complement([(av, av)]))
        if op is C.ANY:
            if flags & re.DOTALL:
                return '(?s:.)'
            return '[^\\x{A}]'
        if op is C.IN:
            return self.in_set(av, flags)
        if op is C.BRANCH:
            _, alts = av
            return '(?:' + '|'.join(self.seq(a, flags, trailing) for a in alts) + ')'
        if op is C.SUBPATTERN:
            gid, add, dele, p = av
            nflags = (flags | add) & ~dele
            if gid is None:
                return '(?:' + self.seq(p, nflags, trailing) + ')'
            self.gmap[gid] = self.next_group
            self.next_group += 1
            self.capture_depth += 1
            body = self.seq(p, nflags, trailing)
            self.capture_depth -= 1
            return '(' + body + ')'
        if op in (C.MAX_REPEAT, C.MIN_REPEAT, POSSESSIVE_REPEAT):
            lo, hi, p = av
            body = '(?:' + self.seq(p, flags) + ')'
            if hi is C.MAXREPEAT or hi == C.MAXREPEAT:
                q = {0: '*', 1: '+'}.get(lo, '{%d,}' % lo)
            elif lo == 0 and hi == 1:
                q = '?'
            elif lo == hi:
                q = '{%d}' % lo
            else:
                q = '{%d,%d}' % (lo, hi)
            if op is C.MIN_REPEAT:
                return body + q + '?'
            if op is POSSESSIVE_REPEAT:
                if self.mode == 'pre':
                    return body + q
                self.fancy = True
                return '(?>' + body + q + ')'
            return body + q
        if op is C.AT:
            return self.at(av, flags)
        if op in (C.ASSERT, C.ASSERT_NOT):
            direction, p = av
            if self.mode == 'pre':
                # superset: drop the assertion (the prefilter never reads groups)
                return ''
            # A lookahead inside a capture group cannot become a group: the
            # enclosing group would then also span the lookahead text.
            if (self.mode == 'x' and trailing and op is C.ASSERT and direction == 1
                    and not self.capture_depth):
                idx = self.next_group
                self.next_group += 1
                self.la_groups.append(idx)
                return '(' + self.seq(p, flags) + ')'
            self.fancy = True
            body = self.seq(p, flags)
            if direction == 1:
                return ('(?=' if op is C.ASSERT else '(?!') + body + ')'
            return ('(?<=' if op is C.ASSERT else '(?<!') + body + ')'
        if op is C.GROUPREF:
            if self.mode == 'pre':
                return '(?s:.*?)'
            self.fancy = True
            if flags & re.IGNORECASE:
                raise Unsupported('case-insensitive backreference')
            return '\\k<%d>' % av
        if op is C.GROUPREF_EXISTS:
            gid, yes, no = av
            ys = self.seq(yes, flags)
            ns = self.seq(no, flags) if no is not None else ''
            if self.mode == 'pre':
                return '(?:' + ys + '|' + ns + ')'
            self.fancy = True
            return '(?(%d)' % gid + ys + '|' + ns + ')'
        if op is ATOMIC_GROUP:
            body = self.seq(av, flags)
            if self.mode == 'pre':
                return '(?:' + body + ')'
            self.fancy = True
            return '(?>' + body + ')'
        raise Unsupported(f'regex op {op}')

    def in_set(self, items, flags):
        neg = False
        has_cat = False
        ranges = []
        for op, av in items:
            if op is C.NEGATE:
                neg = True
            elif op is C.LITERAL:
                ranges.append((av, av))
            elif op is C.RANGE:
                ranges.append(tuple(av))
            elif op is C.CATEGORY:
                has_cat = True
                ranges.extend(category_ranges(av, flags))
            else:
                raise Unsupported(f'set item {op}')
        if flags & re.IGNORECASE:
            # Python's own case-insensitive semantics, computed exhaustively.
            return icase_class(py_set(items), flags)
        if neg:
            ranges = complement(ranges)
        if has_cat:
            return sym_class(py_set(items), flags, lambda: ranges)
        return emit_set(ranges)

    def single_char_set(self, p, flags):
        """Class text for a lookbehind body that is exactly one character."""
        items = list(p)
        if len(items) != 1 or flags & re.IGNORECASE:
            return None
        op, av = items[0]
        if op is C.LITERAL:
            return emit_set([(av, av)])
        if op is C.NOT_LITERAL:
            return emit_set(complement([(av, av)]))
        if op is C.ANY:
            return emit_set(complement([]) if flags & re.DOTALL else complement([(10, 10)]))
        if op is C.IN:
            try:
                return self.in_set(av, flags)
            except Unsupported:
                return None
        return None

    def at(self, code, flags):
        if code is C.AT_BEGINNING:
            return '(?m:^)' if flags & re.MULTILINE else '\\A'
        if code is C.AT_BEGINNING_STRING:
            return '\\A'
        if code is C.AT_END:
            if flags & re.MULTILINE:
                return '(?m:$)'
            # Python: end of string, or before a final newline.
            if self.mode == 'pre':
                return ''
            self.fancy = True
            return '(?=\\x{A}?\\z)'
        if code is C.AT_END_STRING:
            return '\\z'
        if code in (C.AT_BOUNDARY, C.AT_NON_BOUNDARY):
            self.has_boundary = True
            b = code is C.AT_BOUNDARY
            if flags & re.ASCII:
                return '(?-u:\\b)' if b else '(?-u:\\B)'
            if self.mode == 'd':
                self.fancy = True
                w = self.word_class(flags)
                if b:
                    return '(?:(?<=%s)(?!%s)|(?<!%s)(?=%s))' % (w, w, w, w)
                return '(?:(?<=%s)(?=%s)|(?<!%s)(?!%s))' % (w, w, w, w)
            if self.mode == 'pre':
                return ''
            return '\\b' if b else '\\B'
        raise Unsupported(f'anchor {code}')


def translate(pattern):
    """Return the IR regex record for a compiled Python pattern."""
    flags = pattern.flags
    if isinstance(pattern.pattern, bytes):
        raise Unsupported('bytes pattern')
    if flags & re.LOCALE:
        raise Unsupported('re.LOCALE')
    tree = P.parse(pattern.pattern, flags)
    gflags = tree.state.flags
    en = Emitter('n')
    rn = en.seq(tree, gflags)
    # `src` is informational; lone surrogates in it would not survive JSON
    # readers that insist on valid UTF-16 escapes.
    src = ''.join(r'\u%04x' % ord(c) if 0xD800 <= ord(c) <= 0xDFFF else c for c in pattern.pattern)
    rec = {'src': src, 'flags': int(gflags), 'groups': pattern.groups,
           're': rn, 'fancy': en.fancy}
    if en.has_boundary and not (gflags & re.ASCII):
        ed = Emitter('d')
        rec['re_d'] = ed.seq(tree, gflags)
    if en.fancy or 're_d' in rec:
        ep = Emitter('pre')
        pre = ep.seq(tree, gflags)
        assert not ep.fancy
        rec['pre'] = pre
    if en.fancy:
        ex = Emitter('x')
        rx = ex.seq(tree, gflags, trailing=True, top=True)
        if not ex.fancy and (ex.la_groups or ex.lookbehind):
            rec['rx'] = rx
            rec['rx_map'] = [ex.gmap[g] for g in range(1, pattern.groups + 1)]
            rec['rx_la'] = ex.la_groups
            if ex.lookbehind:
                rec['rx_lb'] = ex.lookbehind
    return rec


ALL_CHARS = None
ICASE_CACHE = {}


def python_icase_ranges(py_pattern, flags):
    """Every code point that the one-character Python pattern matches under
    re.IGNORECASE (with the pattern's other flags), by scanning all of Unicode
    with Python's own regex engine."""
    global ALL_CHARS
    key = (py_pattern, flags & (re.ASCII | re.IGNORECASE))
    if key in ICASE_CACHE:
        return ICASE_CACHE[key]
    if ALL_CHARS is None:
        ALL_CHARS = ''.join(chr(c) for c in range(MAXCP + 1) if not SURR[0] <= c <= SURR[1])
    rx = re.compile(py_pattern, re.IGNORECASE | (flags & re.ASCII))
    cps = sorted({ord(m.group()) for m in rx.finditer(ALL_CHARS)})
    ICASE_CACHE[key] = normalize(to_ranges(cps))
    return ICASE_CACHE[key]


def icase_class(py_pattern, flags):
    return sym_class(py_pattern, flags | re.IGNORECASE,
                     lambda: python_icase_ranges(py_pattern, flags))


def py_literal(cp):
    return '\\U%08x' % cp


def py_set(items):
    cats = {
        C.CATEGORY_DIGIT: '\\d', C.CATEGORY_NOT_DIGIT: '\\D',
        C.CATEGORY_SPACE: '\\s', C.CATEGORY_NOT_SPACE: '\\S',
        C.CATEGORY_WORD: '\\w', C.CATEGORY_NOT_WORD: '\\W',
    }
    out = '['
    for op, av in items:
        if op is C.NEGATE:
            out += '^'
        elif op is C.LITERAL:
            out += py_literal(av)
        elif op is C.RANGE:
            out += py_literal(av[0]) + '-' + py_literal(av[1])
        elif op is C.CATEGORY:
            out += cats[av]
        else:
            raise Unsupported(f'set item {op}')
    return out + ']'


# ---------------------------------------------------------------------------
# actions and state transitions

TOKENS = {}


def tokid(tt):
    assert type(tt) is _TokenType
    if tt not in TOKENS:
        TOKENS[tt] = len(TOKENS)
    return TOKENS[tt]


def closure_vars(fn):
    names = fn.__code__.co_freevars
    cells = fn.__closure__ or ()
    return {n: c.cell_contents for n, c in zip(names, cells)}


def conv_using(fn, lexcls, native_names):
    cv = closure_vars(fn)
    kwargs = cv.get('kwargs', {})
    gt = cv.get('gt_kwargs', {})
    if kwargs:
        raise Unsupported(f'using(...) with lexer options {kwargs!r}')
    stack = list(gt['stack']) if 'stack' in gt else None
    if '_other' in cv:
        other = cv['_other']
        if other is lexcls:
            target = 'this'
        elif other.__name__ in native_names:
            target = other.__name__
        else:
            raise Unsupported(f'using({other.__name__}) delegates to a lexer outside the native set')
        return {'k': 'using', 'lexer': target, 'stack': stack}
    return {'k': 'using', 'lexer': 'this', 'stack': stack}


def conv_action(action, lexcls, native_names, in_group=False):
    if action is None:
        return None
    if type(action) is _TokenType:
        return {'k': 'tok', 't': tokid(action)}
    if callable(action):
        qn = getattr(action, '__qualname__', '')
        if qn == 'bygroups.<locals>.callback':
            if in_group:
                raise Unsupported('bygroups nested inside bygroups')
            args = closure_vars(action)['args']
            groups = []
            for a in args:
                if a is None:
                    groups.append(None)
                else:
                    groups.append(conv_action(a, lexcls, native_names, in_group=True))
            return {'k': 'bygroups', 'g': groups}
        if qn == 'using.<locals>.callback':
            return conv_using(action, lexcls, native_names)
        raise Unsupported(f'callback {qn or action!r}')
    raise Unsupported(f'action {action!r}')


def conv_new_state(ns):
    if ns is None:
        return None
    if isinstance(ns, int):
        return {'k': 'pop', 'n': -ns}
    if ns == '#push':
        return {'k': 'pushtop'}
    if isinstance(ns, tuple):
        return {'k': 'tuple', 'items': list(ns)}
    raise Unsupported(f'new state {ns!r}')


def check_regexlexer(cls):
    if not issubclass(cls, RegexLexer):
        return 'not a RegexLexer (hand-written get_tokens_unprocessed)'
    if issubclass(cls, ExtendedRegexLexer):
        return 'ExtendedRegexLexer (context callbacks)'
    if cls.get_tokens_unprocessed is not RegexLexer.get_tokens_unprocessed:
        return 'overrides get_tokens_unprocessed'
    if cls.get_tokens is not Lexer.get_tokens:
        return 'overrides get_tokens'
    if cls._preprocess_lexer_input is not Lexer._preprocess_lexer_input:
        return 'overrides _preprocess_lexer_input'
    if cls.__init__ is not Lexer.__init__:
        return 'overrides __init__ (lexer options change behaviour)'
    if getattr(cls, 'token_variants', False):
        return 'token_variants'
    return None


def extract_lexer(cls, native_names):
    info = {
        'name': cls.name,
        'aliases': list(cls.aliases),
        'filenames': list(cls.filenames),
        'mimetypes': list(cls.mimetypes),
    }
    if cls.__name__ in HANDWRITTEN:
        src = inspect.getsource(cls)
        info['backend'] = 'handwritten'
        info['handwritten'] = HANDWRITTEN[cls.__name__]
        info['source_sha256'] = hashlib.sha256(src.encode()).hexdigest()
        info['reason'] = 'not a RegexLexer; hand port of get_tokens_unprocessed'
        return info
    why = check_regexlexer(cls)
    if why:
        info['backend'] = 'fallback'
        info['reason'] = why
        return info
    cls()  # RegexLexerMeta.__call__ processes the token definitions
    tokendefs = cls._tokens
    states = {}
    try:
        for sname, rules in tokendefs.items():
            out = []
            for rexmatch, action, new_state in rules:
                pat = rexmatch.__self__
                rec = translate(pat)
                rec['action'] = conv_action(action, cls, native_names)
                rec['next'] = conv_new_state(new_state)
                out.append(rec)
            states[sname] = out
    except Unsupported as e:
        info['backend'] = 'fallback'
        info['reason'] = str(e)
        return info
    info['backend'] = 'native'
    info['states'] = states
    return info


def metadata(cls):
    return {'name': cls.name, 'aliases': list(cls.aliases),
            'filenames': list(cls.filenames), 'mimetypes': list(cls.mimetypes)}


def using_targets(info):
    for rules in info['states'].values():
        for r in rules:
            acts = [r['action']]
            if r['action'] and r['action']['k'] == 'bygroups':
                acts = r['action']['g']
            for a in acts:
                if a and a['k'] == 'using' and a['lexer'] != 'this':
                    yield a['lexer']


def extract_all(names, allowed, reasons):
    """Extract the lexers in `allowed`; the others get `reasons[name]`.

    A lexer that delegates (using) to a lexer that is not native cannot be
    native either; that demotion restarts the extraction with fresh tables,
    so no class or token type of a demoted lexer stays behind.
    """
    global CLASSES, FIXED_INDEX, SYM_INDEX, TOKENS
    allowed = set(allowed)
    while True:
        CLASSES, FIXED_INDEX, SYM_INDEX, TOKENS = [], {}, {}, {}
        lexers = {}
        for name in names:
            cls = getattr(pygments.lexers, name)
            if name in allowed or name in HANDWRITTEN:
                lexers[name] = extract_lexer(cls, set(names))
            else:
                lexers[name] = dict(metadata(cls), backend='fallback',
                                    reason=reasons.get(name, 'not gated'))
        demote = {}
        for name, info in lexers.items():
            if info['backend'] == 'native':
                for t in using_targets(info):
                    if lexers[t]['backend'] not in ('native', 'handwritten'):
                        demote[name] = f'using({t}) and that lexer is not native'
                        break
        if not demote:
            return lexers
        for name, why in demote.items():
            allowed.discard(name)
            reasons[name] = why


def main():
    global CATS
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path,
                    default=Path(__file__).resolve().parent.parent / 'ir' / 'pygments_ir.json',
                    help='IR for this interpreter alone (merge_variants.py combines several)')
    ap.add_argument('--candidates', action='store_true',
                    help='mark every lexer whose tables fit the IR as native, ignoring the gate '
                         'file (the input of tests/gate.py)')
    ap.add_argument('--core-only', action='store_true', help='only the core lexers')
    args = ap.parse_args()
    if pygments.__version__ != PYGMENTS_PIN:
        sys.exit(f'Pygments {PYGMENTS_PIN} required, found {pygments.__version__}')
    CATS = compute_categories()
    import unicodedata
    from pygments.lexers._mapping import LEXERS as MAPPING
    names = sorted(MAPPING)
    if args.core_only:
        allowed, reasons = set(CORE), {}
    elif args.candidates:
        allowed, reasons = set(names), {}
    else:
        gate = json.loads(GATE_FILE.read_text(encoding='utf-8'))
        if gate.get('pygments_version') != PYGMENTS_PIN:
            sys.exit(f'{GATE_FILE} is for Pygments {gate.get("pygments_version")}')
        allowed = {n for n, r in gate['lexers'].items() if r == 'pass'} | set(CORE)
        reasons = {n: r for n, r in gate['lexers'].items() if r != 'pass'}
    lexers = extract_all(names, allowed, reasons)

    fmt = HtmlFormatter()
    # include every standard type so that tokens from fallback paths still map
    for tt in STANDARD_TYPES:
        tokid(tt)
    token_types = [None] * len(TOKENS)
    for tt, i in TOKENS.items():
        token_types[i] = {'name': str(tt), 'css': fmt._get_css_classes(tt)}

    # Everything except `variants` must come out identical on every Python
    # version (merge_variants.py checks it); a variant holds what the
    # interpreter's Unicode database and case rules decide.
    variant = {
        'pythons': ['%d.%d' % sys.version_info[:2]],
        'python_versions': [sys.version.split()[0]],
        'unicode_version': unicodedata.unidata_version,
        'py_word': CATS['word'],
        'py_space': CATS['space'],
        'classes': [[i, c['text'], c['ascii']] for i, c in enumerate(CLASSES) if 'key' in c],
    }
    ir = {
        'ir_version': IR_VERSION,
        'pygments_version': pygments.__version__,
        'notice': pygments_notice(),
        'token_types': token_types,
        'classes': [None if 'key' in c else [c['text'], c['ascii']] for c in CLASSES],
        'class_keys': [c.get('key') for c in CLASSES],
        'variants': [variant],
        'lexers': lexers,
    }
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    # ASCII-only JSON has no line breaks to translate (Path.write_text has no
    # newline= argument before Python 3.10).
    out.write_bytes(json.dumps(ir, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
    from collections import Counter
    by = Counter(info['backend'] for info in lexers.values())
    print(f'{len(lexers)} lexers: ' + ', '.join(f'{k} {v}' for k, v in sorted(by.items())))
    for name in CORE:
        info = lexers[name]
        n = sum(len(v) for v in info.get('states', {}).values())
        print(f"  {name:16} {info['backend']:11} states={len(info.get('states', {})):3} rules={n:4}")
    print('wrote', out, out.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
