//! JSON IR produced by `build_ir/extract.py` from Pygments' processed tables.
//!
//! The IR file carries Pygments' BSD-2-Clause notice (field `notice`).

use serde::Deserialize;
use std::collections::BTreeMap;

pub const IR_JSON: &str = include_str!("../ir/pygments_ir.json");
/// The bulky parts (lexer states, variant class tables), concatenated JSON
/// texts that `IR_JSON` points into with `[offset, length]` pairs, so that
/// loading the registry does not have to scan them.
pub const IR_BULK: &str = include_str!("../ir/pygments_ir.bulk");

pub fn bulk(at: (usize, usize)) -> &'static str {
    &IR_BULK[at.0..at.0 + at.1]
}
pub const SUPPORTED_IR_VERSION: u32 = 3;

#[derive(Deserialize)]
pub struct Ir {
    pub ir_version: u32,
    pub pygments_version: String,
    pub notice: String,
    pub token_types: Vec<TokenTypeIr>,
    /// (full class, ASCII-restricted twin); `None` for a symbolic class, whose
    /// text comes from the selected variant
    pub classes: Vec<Option<(String, String)>>,
    pub variants: Vec<VariantIr>,
    pub lexers: BTreeMap<String, LexerIr>,
}

/// What the interpreter's Unicode database and case rules decide: the members
/// of every symbolic class and Python's `\w` / `\s` sets.
#[derive(Deserialize)]
pub struct VariantIr {
    /// "3.11" style tags of the Python versions this variant was built with
    pub pythons: Vec<String>,
    pub python_versions: Vec<String>,
    pub unicode_version: String,
    pub py_word: Vec<(u32, u32)>,
    pub py_space: Vec<(u32, u32)>,
    /// where the `[index, full, ascii]` triples are in `IR_BULK`; parsed
    /// only for the selected variant
    pub classes_at: (usize, usize),
}

#[derive(Deserialize)]
pub struct TokenTypeIr {
    pub name: String,
    pub css: String,
}

#[derive(Deserialize)]
pub struct LexerIr {
    pub name: String,
    pub aliases: Vec<String>,
    pub filenames: Vec<String>,
    #[serde(default)]
    pub mimetypes: Vec<String>,
    pub backend: String,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub handwritten: Option<String>,
    /// parsed lazily, only for the lexers actually used
    #[serde(default)]
    pub states_at: Option<(usize, usize)>,
}

#[derive(Deserialize)]
pub struct RuleIr {
    pub src: String,
    pub flags: u32,
    pub groups: usize,
    pub re: String,
    pub fancy: bool,
    #[serde(default)]
    pub re_d: Option<String>,
    #[serde(default)]
    pub pre: Option<String>,
    #[serde(default)]
    pub rx: Option<String>,
    #[serde(default)]
    pub rx_map: Option<Vec<usize>>,
    #[serde(default)]
    pub rx_la: Option<Vec<usize>>,
    #[serde(default)]
    pub rx_lb: Option<LookbehindIr>,
    pub action: Option<ActionIr>,
    pub next: Option<NextIr>,
}

#[derive(Deserialize, Clone)]
pub struct LookbehindIr {
    pub neg: bool,
    /// one-character class text (may hold `@N@` placeholders)
    pub set: String,
}

#[derive(Deserialize, Clone)]
#[serde(tag = "k")]
pub enum ActionIr {
    #[serde(rename = "tok")]
    Tok { t: u16 },
    #[serde(rename = "bygroups")]
    ByGroups { g: Vec<Option<ActionIr>> },
    #[serde(rename = "using")]
    Using {
        lexer: String,
        stack: Option<Vec<String>>,
    },
}

#[derive(Deserialize, Clone)]
#[serde(tag = "k")]
pub enum NextIr {
    #[serde(rename = "pop")]
    Pop { n: usize },
    #[serde(rename = "pushtop")]
    PushTop,
    #[serde(rename = "tuple")]
    Tuple { items: Vec<String> },
}

pub fn load() -> Ir {
    let ir: Ir = serde_json::from_str(IR_JSON).expect("embedded IR is valid JSON");
    assert_eq!(ir.ir_version, SUPPORTED_IR_VERSION, "IR version mismatch");
    ir
}
