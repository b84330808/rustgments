"""API checks: backends, fallbacks, drop-in use with Pygments objects.

  python -I tests/test_api.py
"""

import io

import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter as PygHtml

import rustgments as rg

CODE = {
    'python': 'def f(x):\n    return x  # é\n',
    'json': '{"a": [1, 2.5, true, null]}\n',
    'js': 'const re = /a+b/g; let s = `x${y}`;\n',
    'bash': 'for f in *.sh; do echo "$f"; done\n',
    'rust': 'fn main() { let r = r#"raw"#; println!("{}", r); }\n',
}


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    return cond


def main():
    good = True
    for alias, code in CODE.items():
        lx = rg.get_lexer_by_name(alias)
        good &= check(lx.backend == 'native', f'{alias}: backend native ({lx.class_name}, reason={lx.reason})')
        ref = pygments.highlight(code, pygments.lexers.get_lexer_by_name(alias), PygHtml())
        good &= check(rg.highlight(code, lx, rg.HtmlFormatter()) == ref, f'{alias}: highlight == Pygments')
        # drop-in: Pygments lexer and formatter objects are accepted
        good &= check(rg.highlight(code, pygments.lexers.get_lexer_by_name(alias), PygHtml()) == ref,
                      f'{alias}: Pygments objects accepted')
        buf = io.StringIO()
        rg.highlight(code, lx, rg.HtmlFormatter(), outfile=buf)
        good &= check(buf.getvalue() == ref, f'{alias}: outfile')
        toks = lx.get_tokens(code)
        good &= check(toks == list(pygments.lexers.get_lexer_by_name(alias).get_tokens(code)),
                      f'{alias}: get_tokens == Pygments')
    # \b right after a non-ASCII character that Python counts as \w but the
    # lexer emits on its own (Error token): the ASCII-tier DFA must not decide
    # the boundary from the non-ASCII look-behind byte.
    for alias, kws in (('javascript', ['from', 'as', 'if']), ('python', ['if', 'import', 'True']),
                       ('bash', ['if', 'echo', 'then']), ('rust', ['fn', 'let', 'as'])):
        cls = type(pygments.lexers.get_lexer_by_name(alias))
        for ch in ('²', '٣', 'Ⅻ', 'é'):
            for kw in kws:
                text = f'{ch}{kw} x\n'
                exp = list(cls().get_tokens_unprocessed(text))
                got = rg.get_lexer_by_name(alias).get_tokens_unprocessed(text)
                if got != exp:
                    good &= check(False, f'{alias}: boundary after U+{ord(ch):04X} before {kw!r}')
    good &= check(True, 'word boundary after non-ASCII look-behind (checked above)')
    # fallbacks
    yaml = rg.get_lexer_by_name('yaml')
    good &= check(yaml.backend == 'fallback', f'yaml: fallback ({yaml.reason})')
    good &= check(rg.highlight('a: 1\n', yaml, rg.HtmlFormatter()) ==
                  pygments.highlight('a: 1\n', pygments.lexers.get_lexer_by_name('yaml'), PygHtml()),
                  'yaml: fallback output == Pygments')
    opt = rg.get_lexer_by_name('python', stripall=True)
    good &= check(opt.backend == 'fallback', f'python with options: fallback ({opt.reason})')
    code = '  x = 1  \n\n'
    good &= check(rg.highlight(code, opt, rg.HtmlFormatter()) ==
                  pygments.highlight(code, pygments.lexers.PythonLexer(stripall=True), PygHtml()),
                  'python stripall output == Pygments')
    good &= check(rg.highlight(code, rg.Lexer('PythonLexer'), rg.HtmlFormatter(linenos='table')) ==
                  pygments.highlight(code, pygments.lexers.PythonLexer(), PygHtml(linenos='table')),
                  'non-default HtmlFormatter falls back, output == Pygments')
    b = 'x = "é"\n'.encode('utf-8')
    good &= check(rg.highlight(b, rg.Lexer('PythonLexer'), rg.HtmlFormatter()) ==
                  pygments.highlight(b, pygments.lexers.PythonLexer(), PygHtml()),
                  'bytes input falls back, output == Pygments')
    s = 'x = "\ud800"\n'  # lone surrogate: not valid UTF-8 for Rust
    good &= check(rg.highlight(s, rg.Lexer('PythonLexer'), rg.HtmlFormatter()) ==
                  pygments.highlight(s, pygments.lexers.PythonLexer(), PygHtml()),
                  'lone surrogate falls back, output == Pygments')
    print('ALL OK' if good else 'FAILURES')
    raise SystemExit(0 if good else 1)


if __name__ == '__main__':
    main()
