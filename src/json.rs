//! Hand port of `pygments.lexers.data.JsonLexer.get_tokens_unprocessed`
//! (Pygments 2.21.0). JsonLexer is not a RegexLexer, so it cannot go through
//! the IR; the extractor pins the upstream source hash this port follows.

use crate::engine::{Registry, Tok, TokId};

struct Ids {
    string_double: TokId,
    whitespace: TokId,
    constant: TokId,
    float: TokId,
    integer: TokId,
    punctuation: TokId,
    comment_single: TokId,
    comment_multiline: TokId,
    error: TokId,
    name_tag: TokId,
}

fn ids(reg: &Registry) -> Ids {
    let f = |n: &str| -> TokId {
        reg.token_names
            .iter()
            .position(|x| x == n)
            .unwrap_or_else(|| panic!("token {n}")) as TokId
    };
    Ids {
        string_double: f("Token.Literal.String.Double"),
        whitespace: f("Token.Text.Whitespace"),
        constant: f("Token.Keyword.Constant"),
        float: f("Token.Literal.Number.Float"),
        integer: f("Token.Literal.Number.Integer"),
        punctuation: f("Token.Punctuation"),
        comment_single: f("Token.Comment.Single"),
        comment_multiline: f("Token.Comment.Multiline"),
        error: f("Token.Error"),
        name_tag: f("Token.Name.Tag"),
    }
}

#[inline]
fn is_integer(c: char) -> bool {
    matches!(c, '-' | '0'..='9')
}
#[inline]
fn is_float(c: char) -> bool {
    matches!(c, '.' | 'e' | 'E' | '+')
}
#[inline]
fn is_constant(c: char) -> bool {
    // set('truefalsenull')
    matches!(c, 't' | 'r' | 'u' | 'e' | 'f' | 'a' | 'l' | 's' | 'n')
}
#[inline]
fn is_hex(c: char) -> bool {
    c.is_ascii_hexdigit()
}
#[inline]
fn is_punct(c: char) -> bool {
    matches!(c, '{' | '}' | '[' | ']' | ',')
}
#[inline]
fn is_ws(c: char) -> bool {
    matches!(c, ' ' | '\n' | '\r' | '\t')
}

pub fn lex_json(reg: &Registry, text: &str, base: usize, out: &mut Vec<Tok>) {
    let id = ids(reg);
    let mut in_string = false;
    let mut in_escape = false;
    let mut in_unicode_escape: u32 = 0;
    let mut in_whitespace = false;
    let mut in_constant = false;
    let mut in_number = false;
    let mut in_float = false;
    let mut in_punctuation = false;
    let mut in_comment_single = false;
    let mut in_comment_multiline = false;
    let mut expecting_second_comment_opener = false;
    let mut expecting_second_comment_closer = false;
    let mut start = 0usize;
    let mut queue: Vec<Tok> = Vec::new();

    macro_rules! emit {
        ($s:expr, $e:expr, $t:expr) => {
            out.push(Tok {
                start: base + $s,
                end: base + $e,
                tt: $t,
            })
        };
    }
    macro_rules! flush_queue {
        () => {
            out.extend(queue.drain(..));
        };
    }

    for (stop, character) in text.char_indices() {
        let clen = character.len_utf8();
        if in_string {
            if in_unicode_escape != 0 {
                if is_hex(character) {
                    in_unicode_escape -= 1;
                    if in_unicode_escape == 0 {
                        in_escape = false;
                    }
                } else {
                    in_unicode_escape = 0;
                    in_escape = false;
                }
            } else if in_escape {
                if character == 'u' {
                    in_unicode_escape = 4;
                } else {
                    in_escape = false;
                }
            } else if character == '\\' {
                in_escape = true;
            } else if character == '"' {
                queue.push(Tok {
                    start: base + start,
                    end: base + stop + 1,
                    tt: id.string_double,
                });
                in_string = false;
                in_escape = false;
                in_unicode_escape = 0;
            }
            continue;
        } else if in_whitespace {
            if is_ws(character) {
                continue;
            }
            if !queue.is_empty() {
                queue.push(Tok {
                    start: base + start,
                    end: base + stop,
                    tt: id.whitespace,
                });
            } else {
                emit!(start, stop, id.whitespace);
            }
            in_whitespace = false;
        } else if in_constant {
            if is_constant(character) {
                continue;
            }
            emit!(start, stop, id.constant);
            in_constant = false;
        } else if in_number {
            if is_integer(character) {
                continue;
            } else if is_float(character) {
                in_float = true;
                continue;
            }
            if in_float {
                emit!(start, stop, id.float);
            } else {
                emit!(start, stop, id.integer);
            }
            in_number = false;
            in_float = false;
        } else if in_punctuation {
            if is_punct(character) {
                continue;
            }
            emit!(start, stop, id.punctuation);
            in_punctuation = false;
        } else if in_comment_single {
            if character != '\n' {
                continue;
            }
            if !queue.is_empty() {
                queue.push(Tok {
                    start: base + start,
                    end: base + stop,
                    tt: id.comment_single,
                });
            } else {
                emit!(start, stop, id.comment_single);
            }
            in_comment_single = false;
        } else if in_comment_multiline {
            if character == '*' {
                expecting_second_comment_closer = true;
            } else if expecting_second_comment_closer {
                expecting_second_comment_closer = false;
                if character == '/' {
                    if !queue.is_empty() {
                        queue.push(Tok {
                            start: base + start,
                            end: base + stop + 1,
                            tt: id.comment_multiline,
                        });
                    } else {
                        emit!(start, stop + 1, id.comment_multiline);
                    }
                    in_comment_multiline = false;
                }
            }
            continue;
        } else if expecting_second_comment_opener {
            expecting_second_comment_opener = false;
            if character == '/' {
                in_comment_single = true;
                continue;
            } else if character == '*' {
                in_comment_multiline = true;
                continue;
            }
            flush_queue!();
            emit!(start, stop, id.error);
        }

        start = stop;

        if character == '"' {
            in_string = true;
        } else if is_ws(character) {
            in_whitespace = true;
        } else if matches!(character, 'f' | 'n' | 't') {
            flush_queue!();
            in_constant = true;
        } else if is_integer(character) {
            flush_queue!();
            in_number = true;
        } else if character == ':' {
            for t in queue.drain(..) {
                if t.tt == id.string_double {
                    out.push(Tok {
                        tt: id.name_tag,
                        ..t
                    });
                } else {
                    out.push(t);
                }
            }
            in_punctuation = true;
        } else if is_punct(character) {
            flush_queue!();
            in_punctuation = true;
        } else if character == '/' {
            expecting_second_comment_opener = true;
        } else {
            flush_queue!();
            emit!(stop, stop + clen, id.error);
        }
    }

    let n = text.len();
    flush_queue!();
    if in_string {
        emit!(start, n, id.error);
    } else if in_float {
        emit!(start, n, id.float);
    } else if in_number {
        emit!(start, n, id.integer);
    } else if in_constant {
        emit!(start, n, id.constant);
    } else if in_whitespace {
        emit!(start, n, id.whitespace);
    } else if in_punctuation {
        emit!(start, n, id.punctuation);
    } else if in_comment_single {
        emit!(start, n, id.comment_single);
    } else if in_comment_multiline {
        emit!(start, n, id.error);
    } else if expecting_second_comment_opener {
        emit!(start, n, id.error);
    }
}
