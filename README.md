# rustgments

A Rust engine for [Pygments](https://pygments.org/). It runs Pygments' own lexer tables and produces the same tokens and the same HTML, byte for byte. A lexer or option it can't reproduce exactly goes to Pygments itself, so output never changes.

```python
import rustgments

lexer = rustgments.get_lexer_by_name('python')
html = rustgments.highlight(code, lexer, rustgments.HtmlFormatter())
```

Or leave your code alone and make Pygments itself use it:

```python
import rustgments
rustgments.install()   # pygments.highlight() and friends now use the native path
```

## When it helps

It helps when one process highlights a lot of code. Python startup and imports are the same either way, so a one-shot script on a small file gains little.

| Workload | Pygments | rustgments | |
|---|---:|---:|---:|
| `highlight()` called again in the same process, 20 KB - 1.2 MB files | 5 - 1,600 ms | 0.2 - 53 ms | 12 - 75x |
| First `highlight()` of a process, same files | 14 - 1,588 ms | 0.3 - 52 ms | 7 - 54x |
| 4,226 files up to 20 KB through `pygments.highlight` after `install()`, highlight loop | 32.55 s | 2.23 s | 14.6x |
| Same, whole process | 33.41 s | 3.31 s | 10.1x |
| One file, whole process from start to exit, 20 KB - 1.2 MB | 0.14 - 1.7 s | 0.09 - 0.18 s | 1.1 - 9.5x |
| Django docs with Sphinx (4,199 code blocks) | 109.5 s | 100.7 s | within noise |

The Sphinx row is the honest one for documentation builds: highlighting was 3.8 s of a 113 s build, and it dropped to 1.8 s. Sphinx spends its time elsewhere.

The first use of each lexer costs a little more than in Pygments (median 3.8 ms against 2.7 ms over 361 lexers; a few very large lexers take 100-300 ms), so a short run that touches many languages a few times each can come out slower.

Windows 11, i7-9700, CPython 3.11-3.12, base and rustgments alternating. Raw results are in [`bench/results`](bench/results).

## What runs natively

363 of Pygments' 602 lexers, including Python, JavaScript, TypeScript, JSON, Bash, Rust, Go, Java, Kotlin, HTML, CSS, XML, TOML, INI, Dockerfile, PowerShell, Perl and diff. **C, C++, YAML, Markdown, SQL, Ruby, PHP and Swift still run on Pygments** for now.

A lexer stays on Pygments when it isn't a plain `RegexLexer` table (hand-written lexers, custom callbacks, overridden tokenizing), when Pygments has no test files to check it against, or when lexer options change its behaviour. `lexer.backend` tells you which one you got, and `lexer.reason` says why:

```python
>>> rustgments.get_lexer_by_name('c').backend
'fallback'
```

`HtmlFormatter` runs natively with the options Sphinx and pymdownx use (`cssclass`, `nowrap`, `linenos` variants, `hl_lines`, `wrapcode`, `linespans`, `lineanchors`, `noclasses`, `style`, ...). Other formatters, and `tagsfile`, are plain Pygments.

## Requirements

Python 3.9 - 3.14 and **Pygments 2.21.0**. The tables are generated from that exact release, and Python's `\w`, `\d`, `\s` follow the interpreter's Unicode version, so there is one table set per Unicode version. With any other Pygments, or a Python it has no tables for, everything runs on Pygments: correct, just not faster.

## Install

```
pip install rustgments
```

Wheels are built for Windows, Linux (glibc and musl, x86_64 and aarch64) and macOS (x86_64 and arm64), for any Python from 3.9. Elsewhere pip builds from source, which needs a Rust toolchain.

## Using it with existing tools

| | |
|---|---|
| Your own code | `rustgments.install()`; `rustgments.uninstall()` restores Pygments |
| Sphinx | `extensions = ['rustgments.sphinxext']` |
| MkDocs | `plugins: [rustgments]` |
| Anything else | set `RUSTGMENTS_INSTALL=1` |

The environment variable works through a one-line `.pth` file in the wheel. It runs at every Python startup, and all it does is check that variable. Delete `rustgments-autoinstall.pth` from `site-packages` if you'd rather not have it.

`install()` patches `get_tokens`, `get_tokens_unprocessed` of the native lexers and the per-token loop of `HtmlFormatter`. Line numbers, highlighted lines, anchors and wrapping still run Pygments' own code.

## How parity is checked

Parity is the point of this project, so a lexer only runs natively after passing a gate on every supported Python:

- `tests/gate.py`: each lexer's own Pygments test files (`tests/examplefiles`, `tests/snippets`) plus 30 generated mutants, comparing preprocessing, every `(offset, token, value)` and the final HTML. Any mismatch, engine error or timeout keeps that lexer on Pygments. The result is [`build_ir/native_gate.json`](build_ir/native_gate.json).
- `tests/parity.py`: the same comparisons over a corpus of real code (5,171 files, 88 MB, for the five core lexers).
- `tests/independent_check.py`: a second checker using only the public API, on other files and random mutants.
- `tests/dropin_check.py`: Pygments' API before and after `install()`, with 23 formatter and lexer call shapes.
- `tests/unicode_check.py`: the 9,917 characters whose classes changed between Unicode versions, inserted into real code.

The corpus isn't in this repository. Point `RUSTGMENTS_CORPUS` at a directory with `python/`, `json/`, `js/`, `bash/` and `rust/` subdirectories, and `RUSTGMENTS_PYGMENTS_SRC` at a checkout of Pygments 2.21.0.

CI builds every wheel and runs Pygments' own test inputs against it.

## How it works

A build step imports Pygments and serializes each lexer's processed token tables, so `include`, `inherit`, `words()` and friends are already resolved. Every pattern is re-emitted from Python's own regex parse tree into Rust syntax, with Python's semantics spelled out (`\w`, `\d`, `\s`, `\Z`, `$`, flags). At run time the engine matches with DFAs where it can and falls back to [fancy-regex](https://github.com/fancy-regex/fancy-regex) for look-around and back-references, following `RegexLexer.get_tokens_unprocessed` step by step.

`build_ir/` regenerates the tables in `ir/`, which are checked in.

## Status

Early. Version 0.1.0 tracks Pygments 2.21.0 only, and the API is the small part of Pygments listed above. Please open an issue if the output ever differs from Pygments: that's the bug this project cares about most.

Prior art: the `pygmentsrs` crate inside [westurner/dsport](https://github.com/westurner/dsport) takes a similar approach for a docutils port. No code from it is used here.

## License

BSD-2-Clause, see [LICENSE](LICENSE). The generated tables are derived from Pygments ([LICENSE-pygments](LICENSE-pygments)). Rust crate licenses are in [LICENSE-third-party.txt](LICENSE-third-party.txt).
