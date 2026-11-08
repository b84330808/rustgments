//! Build `ir/dfa.bin`: minimized dense DFAs for the ASCII tier of every state
//! of the core lexers: the full rule set of each state, plus the suffix
//! sets where a search resumes after an inexact candidate. Other native
//! lexers build lazy DFAs at first use (keeps the blob at a few MB).
//!
//! Run after regenerating the IR, then rebuild the library:
//!   cargo run --release --example build_dfas
use regex_automata::dfa::dense;
use regex_automata::nfa::thompson;
use std::io::Write;

fn main() {
    let reg = rustgments::engine::registry();
    let mut entries: Vec<(String, u64, Vec<u8>)> = Vec::new();
    const PREBUILT: &[&str] = &["PythonLexer", "JavascriptLexer", "BashLexer", "RustLexer"];
    for lx in &reg.lexers {
        if !PREBUILT.contains(&lx.class_name.as_str()) {
            continue;
        }
        let rl = match lx.kind(reg) {
            rustgments::engine::Kind::Regex(rl) => rl,
            _ => continue,
        };
        for st in &rl.states {
            for start in st.resume_points() {
                let key = st.set_key(start);
                let pats = st.ascii_candidates(start);
                let t0 = std::time::Instant::now();
                let r = dense::Builder::new()
                    .configure(rustgments::prebuilt::dense_config())
                    .syntax(regex_automata::util::syntax::Config::new().nest_limit(1000))
                    .thompson(thompson::Config::new().which_captures(thompson::WhichCaptures::None))
                    .build_many(&pats);
                match r {
                    Ok(dfa) => {
                        let (bytes, pad) = dfa.to_bytes_little_endian();
                        let bytes = bytes[pad..].to_vec();
                        eprintln!(
                            "{:40} rules={:3} {:8} bytes {:6.1} ms",
                            key,
                            pats.len(),
                            bytes.len(),
                            t0.elapsed().as_secs_f64() * 1e3
                        );
                        entries.push((
                            key.clone(),
                            rustgments::prebuilt::hash_patterns(&pats),
                            bytes,
                        ));
                    }
                    Err(e) => eprintln!("{:40} skipped: {e}", key),
                }
            }
        }
    }
    let mut header: Vec<u8> = Vec::new();
    header.extend_from_slice(b"RGDFA001");
    header.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    let mut payload: Vec<u8> = Vec::new();
    for (key, hash, bytes) in &entries {
        while payload.len() % 8 != 0 {
            payload.push(0);
        }
        header.extend_from_slice(&(key.len() as u32).to_le_bytes());
        header.extend_from_slice(key.as_bytes());
        header.extend_from_slice(&hash.to_le_bytes());
        header.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        header.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
        payload.extend_from_slice(bytes);
    }
    let out = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("ir")
        .join("dfa.bin");
    let mut f = std::fs::File::create(&out).unwrap();
    f.write_all(&header).unwrap();
    f.write_all(&payload).unwrap();
    eprintln!(
        "wrote {} ({} entries, {} bytes)",
        out.display(),
        entries.len(),
        header.len() + payload.len()
    );
}
