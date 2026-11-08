//! `HtmlFormatter` output: a port of `HtmlFormatter._format_lines` (the
//! per-token loop) plus, for the default options, `_wrap_pre` and `_wrap_div`
//! (Pygments 2.21.0).
//!
//! `format_lines` takes the span opener of every token type and the line
//! separator from the caller, so any option that only changes those (style,
//! `noclasses`, `classprefix`, `debug_token_types`, `lineseparator`) is
//! exact by construction; the Python layer computes them with the formatter's
//! own code and applies the per-line wrappers (line numbers, `hl_lines`,
//! anchors, `<pre>`, `<div>`) with the formatter's own methods.

use crate::engine::{Registry, Tok};

/// Span openers per token type, interned so that equal strings compare equal
/// (Pygments compares the opener strings, `lspan != cspan`).
pub struct Spans {
    pub opener: Vec<String>,
    pub id: Vec<usize>,
    pub lsep: String,
}

impl Spans {
    /// `openers[t]` is the opener of token type id `t` ("" for none).
    pub fn from_openers(openers: &[String], lsep: &str) -> Spans {
        let mut uniq: Vec<String> = Vec::new();
        let mut id = Vec::with_capacity(openers.len());
        for s in openers {
            let i = match uniq.iter().position(|u| u == s) {
                Some(i) => i,
                None => {
                    uniq.push(s.clone());
                    uniq.len() - 1
                }
            };
            id.push(i);
        }
        Spans {
            opener: uniq,
            id,
            lsep: lsep.to_string(),
        }
    }

    /// Default `HtmlFormatter()` openers: `<span class="CSS">`.
    pub fn new(reg: &Registry) -> Spans {
        let openers: Vec<String> = reg
            .token_css
            .iter()
            .map(|css| {
                if css.is_empty() {
                    String::new()
                } else {
                    format!("<span class=\"{css}\">")
                }
            })
            .collect();
        Spans::from_openers(&openers, "\n")
    }
}

#[inline]
fn push_escaped(out: &mut String, s: &str) {
    // html.escape(s, quote=False)
    let bytes = s.as_bytes();
    let mut last = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let rep = match b {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'>' => "&gt;",
            _ => continue,
        };
        out.push_str(&s[last..i]);
        out.push_str(rep);
        last = i + 1;
    }
    out.push_str(&s[last..]);
}

/// `HtmlFormatter._format_lines`: one string per yielded line (each ends with
/// the line separator). `emit` receives every finished line.
pub fn format_lines<F: FnMut(&str)>(spans: &Spans, text: &str, toks: &[Tok], mut emit: F) {
    let lsep = spans.lsep.as_str();
    let mut line = String::new();
    let mut line_open = false; // Python: `line` list is non-empty
    let mut lspan: &str = ""; // the open span opener
    let mut lspan_id: usize = spans
        .opener
        .iter()
        .position(|s| s.is_empty())
        .unwrap_or(usize::MAX);
    for t in toks {
        let cid = spans.id[t.tt as usize];
        let cspan = spans.opener[cid].as_str();
        let value = &text[t.start..t.end];
        let mut parts = value.split('\n');
        let mut part = parts.next().unwrap_or("");
        // all parts but the last
        for nxt in parts {
            if line_open {
                if lspan_id != cid && !part.is_empty() {
                    if !lspan.is_empty() {
                        line.push_str("</span>");
                    }
                    line.push_str(cspan);
                    push_escaped(&mut line, part);
                    if !cspan.is_empty() {
                        line.push_str("</span>");
                    }
                } else {
                    push_escaped(&mut line, part);
                    if !lspan.is_empty() {
                        line.push_str("</span>");
                    }
                }
                line.push_str(lsep);
                emit(&line);
                line.clear();
                line_open = false;
            } else {
                if !part.is_empty() {
                    line.push_str(cspan);
                    push_escaped(&mut line, part);
                    if !cspan.is_empty() {
                        line.push_str("</span>");
                    }
                }
                line.push_str(lsep);
                emit(&line);
                line.clear();
            }
            part = nxt;
        }
        // the last part
        let last = part;
        if line_open && !last.is_empty() {
            if lspan_id != cid {
                if !lspan.is_empty() {
                    line.push_str("</span>");
                }
                line.push_str(cspan);
                push_escaped(&mut line, last);
                lspan_id = cid;
                lspan = cspan;
            } else {
                push_escaped(&mut line, last);
            }
        } else if !last.is_empty() {
            line.push_str(cspan);
            push_escaped(&mut line, last);
            line_open = true;
            lspan_id = cid;
            lspan = cspan;
        }
    }
    if line_open {
        if !lspan.is_empty() {
            line.push_str("</span>");
        }
        line.push_str(lsep);
        emit(&line);
    }
}

/// Format tokens exactly like `HtmlFormatter().format` (default options).
pub fn format_html(spans: &Spans, text: &str, toks: &[Tok]) -> String {
    let mut out = String::with_capacity(text.len() * 3 + 64);
    out.push_str("<div class=\"highlight\"><pre><span></span>");
    format_lines(spans, text, toks, |l| out.push_str(l));
    out.push_str("</pre></div>\n");
    out
}
