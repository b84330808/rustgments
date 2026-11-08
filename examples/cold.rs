//! Cold-start probe: time registry load and first-use compilation.
use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cls = &args[1];
    let path = &args[2];
    let code = std::fs::read_to_string(path).unwrap();
    let t0 = Instant::now();
    let reg = rustgments::engine::registry();
    let t1 = Instant::now();
    let text = rustgments::engine::preprocess(&code);
    let n = rustgments::tokens_unprocessed(cls, &text).unwrap().len();
    let t2 = Instant::now();
    let n2 = rustgments::tokens_unprocessed(cls, &text).unwrap().len();
    let t3 = Instant::now();
    let h = rustgments::highlight_html(cls, &code).unwrap();
    let t4 = Instant::now();
    eprintln!(
        "registry {:.1} ms, first lex {:.1} ms ({n} toks), second lex {:.1} ms ({n2}), html {:.1} ms ({} bytes), lexers {}",
        (t1 - t0).as_secs_f64() * 1e3,
        (t2 - t1).as_secs_f64() * 1e3,
        (t3 - t2).as_secs_f64() * 1e3,
        (t4 - t3).as_secs_f64() * 1e3,
        h.len(),
        reg.lexers.len()
    );
}
