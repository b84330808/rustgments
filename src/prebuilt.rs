//! Precompiled dense DFAs for the ASCII tier of each state's full rule set.
//!
//! `examples/build_dfas.rs` writes `ir/dfa.bin`; it is embedded here. Each
//! entry is keyed by "<LexerClass>/<state>" and carries a hash of the exact
//! candidate pattern list it was built from. A missing entry or a hash
//! mismatch (IR regenerated without rebuilding the blob) falls back to
//! building a lazy DFA at runtime, so a stale blob can cost time but never
//! change results.
//!
//! Blob layout (little endian):
//!   b"RGDFA001", u32 count,
//!   count x { u32 key_len, key bytes, u64 hash, u64 offset, u64 len },
//!   payload: DFAs from `dense::DFA::to_bytes_little_endian`, each at an
//!   offset that is a multiple of 8 from the start of the payload.

use regex_automata::dfa::dense;
use std::collections::HashMap;
use std::sync::OnceLock;

static BLOB: &[u8] = include_bytes!("../ir/dfa.bin");
const MAGIC: &[u8; 8] = b"RGDFA001";

struct Entry {
    hash: u64,
    offset: usize,
    len: usize,
}

struct Index {
    entries: HashMap<String, Entry>,
    payload_start: usize,
}

fn index() -> Option<&'static Index> {
    static IDX: OnceLock<Option<Index>> = OnceLock::new();
    IDX.get_or_init(|| parse_index(BLOB)).as_ref()
}

fn rd_u32(b: &[u8], at: &mut usize) -> Option<u32> {
    let v = u32::from_le_bytes(b.get(*at..*at + 4)?.try_into().ok()?);
    *at += 4;
    Some(v)
}

fn rd_u64(b: &[u8], at: &mut usize) -> Option<u64> {
    let v = u64::from_le_bytes(b.get(*at..*at + 8)?.try_into().ok()?);
    *at += 8;
    Some(v)
}

fn parse_index(b: &[u8]) -> Option<Index> {
    if b.len() < 12 || &b[..8] != MAGIC {
        return None;
    }
    let mut at = 8;
    let n = rd_u32(b, &mut at)? as usize;
    let mut entries = HashMap::with_capacity(n);
    for _ in 0..n {
        let kl = rd_u32(b, &mut at)? as usize;
        let key = std::str::from_utf8(b.get(at..at + kl)?).ok()?.to_string();
        at += kl;
        let hash = rd_u64(b, &mut at)?;
        let offset = rd_u64(b, &mut at)? as usize;
        let len = rd_u64(b, &mut at)? as usize;
        entries.insert(key, Entry { hash, offset, len });
    }
    Some(Index {
        entries,
        payload_start: at,
    })
}

/// FNV-1a over the candidate patterns, with a separator between patterns.
pub fn hash_patterns(pats: &[String]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for p in pats {
        for &b in p.as_bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        h ^= 0xff;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

/// The prebuilt DFA for `key`, if present and built from exactly `pats`.
pub fn lookup(key: &str, pats: &[String]) -> Option<dense::DFA<&'static [u32]>> {
    let idx = index()?;
    let e = idx.entries.get(key)?;
    if e.hash != hash_patterns(pats) {
        return None;
    }
    let start = idx.payload_start + e.offset;
    let bytes = BLOB.get(start..start + e.len)?;
    // include_bytes! gives no alignment guarantee: copy into u32 storage.
    let mut words = vec![0u32; bytes.len().div_ceil(4)];
    // SAFETY: plain byte copy into a buffer of at least `bytes.len()` bytes.
    unsafe {
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), words.as_mut_ptr() as *mut u8, bytes.len());
    }
    let words: &'static [u32] = Box::leak(words.into_boxed_slice());
    // SAFETY: reinterpreting u32 storage as bytes; length and alignment are valid.
    let raw: &'static [u8] =
        unsafe { std::slice::from_raw_parts(words.as_ptr() as *const u8, bytes.len()) };
    let (dfa, _) = dense::DFA::from_bytes(raw).ok()?; // validates the tables
    Some(dfa)
}

/// Configuration shared by the builder and the runtime fallback.
pub fn dense_config() -> dense::Config {
    let mut cfg = dense::Config::new()
        .start_kind(regex_automata::dfa::StartKind::Anchored)
        .unicode_word_boundary(true)
        .minimize(true)
        .dfa_size_limit(Some(64 << 20))
        .determinize_size_limit(Some(256 << 20));
    for b in 0x80..=0xFFu8 {
        cfg = cfg.quit(b, true);
    }
    cfg
}
