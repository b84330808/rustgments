//! RegexLexer engine: a port of `pygments.lexer.RegexLexer.get_tokens_unprocessed`
//! (Pygments 2.21.0) driven by the JSON IR.
//!
//! Offsets inside the engine are UTF-8 byte offsets; the Python layer converts
//! them to code point offsets.
//!
//! Rule selection.  Pygments tries the rules of the current state in order and
//! takes the first whose regex matches at `pos` (`pattern.match(text, pos)`).
//! Here every state gets a multi-pattern regex over its rules (an anchored,
//! leftmost-first search reports the first rule that matches), with two tiers:
//!
//! * the ASCII tier: a lazy DFA over the rules with every large Unicode class
//!   cut down to its ASCII part, configured to quit on any non-ASCII byte.  A
//!   result that comes back without quitting only looked at ASCII bytes, where
//!   both versions of every class agree, so it is exact;
//! * the full tier (on a quit): the same set with the full Unicode classes.
//!
//! Rules that need look-around or backreferences (fancy-regex) sit in the sets
//! as a superset pattern (assertions dropped); when one is reported, the real
//! fancy pattern decides, and on failure the search resumes after that rule.

use crate::ir::{self, ActionIr, NextIr, RuleIr};
use regex_automata::hybrid::dfa::{Cache as HCache, DFA as HDFA};
use regex_automata::nfa::thompson::{self, WhichCaptures};
use regex_automata::util::pool::Pool;
use regex_automata::{meta, Anchored, Input, MatchErrorKind};
use std::collections::HashMap;
use std::sync::OnceLock;

pub type TokId = u16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tok {
    pub start: usize,
    pub end: usize,
    pub tt: TokId,
}

#[derive(Debug)]
pub struct EngineError(pub String);

// ---------------------------------------------------------------------------
// class tables

struct Classes {
    full: Vec<String>,
    ascii: Vec<String>,
}

static CLASSES: OnceLock<Classes> = OnceLock::new();

fn classes() -> &'static Classes {
    CLASSES
        .get()
        .expect("classes are set when the registry is built")
}

/// Expand `@N@` placeholders with the full or ASCII-restricted class text.
fn resolve(src: &str, ascii: bool) -> String {
    if !src.contains('@') {
        return src.to_string();
    }
    let tab = if ascii {
        &classes().ascii
    } else {
        &classes().full
    };
    let mut out = String::with_capacity(src.len() + 1024);
    let mut rest = src;
    while let Some(i) = rest.find('@') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        let j = after.find('@').expect("unterminated class placeholder");
        let n: usize = after[..j].parse().expect("bad class placeholder");
        out.push_str(&tab[n]);
        rest = &after[j + 1..];
    }
    out.push_str(rest);
    out
}

// ---------------------------------------------------------------------------
// single patterns

pub enum Compiled {
    Meta(meta::Regex),
    Fancy(fancy_regex::Regex),
}

/// A pattern handle. Identical patterns (included rules repeat across
/// states) share one compiled regex.
#[derive(Clone)]
pub struct Pat(std::sync::Arc<PatInner>);

pub struct PatInner {
    /// pattern text with `@N@` class placeholders
    pub src: String,
    pub fancy: bool,
    pub ascii: bool,
    cell: OnceLock<Result<Compiled, String>>,
    caps: OnceLock<Result<CapsRe, String>>,
}

impl std::ops::Deref for Pat {
    type Target = PatInner;
    fn deref(&self) -> &PatInner {
        &self.0
    }
}

/// Capture extraction for a non-fancy pattern whose match end is already
/// known: an anchored search on the span `pos..end` (look-around still sees
/// the whole text). Restricting the span cannot change which path wins: the
/// winning path ends at `end` and every higher-priority path already failed.
pub struct CapsRe {
    bt: regex_automata::nfa::thompson::backtrack::BoundedBacktracker,
    pike: regex_automata::nfa::thompson::pikevm::PikeVM,
    bt_cache: Pool<regex_automata::nfa::thompson::backtrack::Cache, BtCacheFn>,
    pike_cache: Pool<regex_automata::nfa::thompson::pikevm::Cache, PikeCacheFn>,
}

type BtCacheFn = Box<
    dyn Fn() -> regex_automata::nfa::thompson::backtrack::Cache
        + Send
        + Sync
        + std::panic::UnwindSafe
        + std::panic::RefUnwindSafe,
>;
type PikeCacheFn = Box<
    dyn Fn() -> regex_automata::nfa::thompson::pikevm::Cache
        + Send
        + Sync
        + std::panic::UnwindSafe
        + std::panic::RefUnwindSafe,
>;

impl CapsRe {
    fn build(src: &str) -> Result<CapsRe, String> {
        use regex_automata::nfa::thompson::{backtrack::BoundedBacktracker, pikevm::PikeVM};
        let t0 = std::time::Instant::now();
        let nfa = thompson::Compiler::new()
            .configure(thompson::Config::new().nfa_size_limit(Some(64 << 20)))
            .build(src)
            .map_err(|e| format!("regex crate cannot compile pattern: {e}"))?;
        let bt = BoundedBacktracker::new_from_nfa(nfa.clone()).map_err(|e| e.to_string())?;
        let pike = PikeVM::new_from_nfa(nfa).map_err(|e| e.to_string())?;
        let (b2, p2) = (bt.clone(), pike.clone());
        let bt_cache = Pool::new(Box::new(move || b2.create_cache()) as BtCacheFn);
        let pike_cache = Pool::new(Box::new(move || p2.create_cache()) as PikeCacheFn);
        if trace() {
            eprintln!(
                "compile caps len={} {:.2} ms",
                src.len(),
                t0.elapsed().as_secs_f64() * 1e3
            );
        }
        Ok(CapsRe {
            bt,
            pike,
            bt_cache,
            pike_cache,
        })
    }

    fn captures(
        &self,
        text: &str,
        pos: usize,
        end: usize,
        groups: &mut Vec<Option<(usize, usize)>>,
    ) -> Result<bool, EngineError> {
        groups.clear();
        let input = Input::new(text).span(pos..end).anchored(Anchored::Yes);
        let ok = if end - pos <= self.bt.max_haystack_len() {
            let mut caps = self.bt.create_captures();
            let mut cache = self.bt_cache.get();
            self.bt
                .try_search(&mut cache, &input, &mut caps)
                .map_err(|e| EngineError(format!("backtracker: {e}")))?;
            for i in 0..caps.group_len() {
                groups.push(caps.get_group(i).map(|s| (s.start, s.end)));
            }
            caps.is_match()
        } else {
            let mut caps = self.pike.create_captures();
            let mut cache = self.pike_cache.get();
            self.pike.search(&mut cache, &input, &mut caps);
            for i in 0..caps.group_len() {
                groups.push(caps.get_group(i).map(|s| (s.start, s.end)));
            }
            caps.is_match()
        };
        Ok(ok && groups[0] == Some((pos, end)))
    }
}

const FANCY_BACKTRACK_LIMIT: usize = 50_000_000;

impl Pat {
    pub fn new(src: String, fancy: bool, ascii: bool) -> Pat {
        type Key = (String, bool, bool);
        static INTERN: OnceLock<std::sync::Mutex<HashMap<Key, Pat>>> = OnceLock::new();
        let map = INTERN.get_or_init(|| std::sync::Mutex::new(HashMap::new()));
        let mut map = map.lock().unwrap();
        map.entry((src.clone(), fancy, ascii))
            .or_insert_with(|| {
                Pat(std::sync::Arc::new(PatInner {
                    src,
                    fancy,
                    ascii,
                    cell: OnceLock::new(),
                    caps: OnceLock::new(),
                }))
            })
            .clone()
    }

    /// Groups of the match known to span `pos..end` (non-fancy patterns).
    pub fn caps_known_end(
        &self,
        text: &str,
        pos: usize,
        end: usize,
        groups: &mut Vec<Option<(usize, usize)>>,
    ) -> Result<bool, EngineError> {
        if self.fancy {
            return Ok(self.caps_at(text, pos, groups)? == Some(end));
        }
        let c = self
            .caps
            .get_or_init(|| CapsRe::build(&resolve(&self.src, self.ascii)))
            .as_ref()
            .map_err(|e| EngineError(e.clone()))?;
        c.captures(text, pos, end, groups)
    }

    pub fn compiled(&self) -> Result<&Compiled, EngineError> {
        let r = self
            .cell
            .get_or_init(|| compile_single(&resolve(&self.src, self.ascii), self.fancy));
        r.as_ref().map_err(|e| EngineError(e.clone()))
    }

    /// Python `pattern.match(text, pos)`: anchored at `pos`; look-behind and
    /// `^` see the text before `pos`. Returns the match end.
    pub fn end_at(&self, text: &str, pos: usize) -> Result<Option<usize>, EngineError> {
        match self.compiled()? {
            Compiled::Meta(re) => {
                let input = Input::new(text)
                    .span(pos..text.len())
                    .anchored(Anchored::Yes);
                Ok(re.search(&input).map(|m| m.end()))
            }
            Compiled::Fancy(re) => match re.find_from_pos(text, pos) {
                Ok(Some(m)) => Ok(Some(m.end())),
                Ok(None) => Ok(None),
                Err(e) => Err(EngineError(format!("fancy-regex runtime error: {e}"))),
            },
        }
    }

    /// Like `end_at`, also filling `groups[i]` with each group's byte span.
    pub fn caps_at(
        &self,
        text: &str,
        pos: usize,
        groups: &mut Vec<Option<(usize, usize)>>,
    ) -> Result<Option<usize>, EngineError> {
        groups.clear();
        match self.compiled()? {
            Compiled::Meta(re) => {
                let input = Input::new(text)
                    .span(pos..text.len())
                    .anchored(Anchored::Yes);
                let mut caps = re.create_captures();
                re.search_captures(&input, &mut caps);
                if !caps.is_match() {
                    return Ok(None);
                }
                for i in 0..caps.group_len() {
                    groups.push(caps.get_group(i).map(|s| (s.start, s.end)));
                }
                Ok(Some(groups[0].unwrap().1))
            }
            Compiled::Fancy(re) => match re.captures_from_pos(text, pos) {
                Ok(Some(c)) => {
                    for i in 0..c.len() {
                        groups.push(c.get(i).map(|m| (m.start(), m.end())));
                    }
                    Ok(Some(groups[0].unwrap().1))
                }
                Ok(None) => Ok(None),
                Err(e) => Err(EngineError(format!("fancy-regex runtime error: {e}"))),
            },
        }
    }
}

fn trace() -> bool {
    static T: OnceLock<bool> = OnceLock::new();
    *T.get_or_init(|| std::env::var_os("RUSTGMENTS_TRACE").is_some())
}

fn compile_single(src: &str, fancy: bool) -> Result<Compiled, String> {
    let t0 = std::time::Instant::now();
    let r = compile_single_inner(src, fancy);
    if trace() {
        eprintln!(
            "compile single fancy={fancy} len={} {:.2} ms",
            src.len(),
            t0.elapsed().as_secs_f64() * 1e3
        );
    }
    r
}

fn compile_single_inner(src: &str, fancy: bool) -> Result<Compiled, String> {
    if fancy {
        // \G anchors the match at the search start: Python `match(text, pos)`.
        let anchored = format!("\\G(?:{src})");
        fancy_regex::RegexBuilder::new(&anchored)
            .backtrack_limit(FANCY_BACKTRACK_LIMIT)
            .build()
            .map(Compiled::Fancy)
            .map_err(|e| format!("fancy-regex cannot compile pattern: {e}"))
    } else {
        meta::Regex::builder()
            .configure(meta::Config::new().nfa_size_limit(Some(64 << 20)))
            .build(src)
            .map(Compiled::Meta)
            .map_err(|e| format!("regex crate cannot compile pattern: {e}"))
    }
}

// ---------------------------------------------------------------------------
// rule sets

/// Parsed patterns, shared by every set that contains them (included rules
/// repeat across states, and suffix sets repeat whole runs of rules).
fn cached_hir(src: &str) -> Result<regex_syntax::hir::Hir, String> {
    static CACHE: OnceLock<std::sync::Mutex<HashMap<String, regex_syntax::hir::Hir>>> =
        OnceLock::new();
    let cache = CACHE.get_or_init(|| std::sync::Mutex::new(HashMap::new()));
    if let Some(h) = cache.lock().unwrap().get(src) {
        return Ok(h.clone());
    }
    let h = regex_syntax::ParserBuilder::new()
        .utf8(true)
        .nest_limit(1000)
        .build()
        .parse(src)
        .map_err(|e| format!("regex crate cannot parse pattern: {e}"))?;
    cache.lock().unwrap().insert(src.to_string(), h.clone());
    Ok(h)
}

type CacheFn =
    Box<dyn Fn() -> HCache + Send + Sync + std::panic::UnwindSafe + std::panic::RefUnwindSafe>;

/// ASCII-tier set: a forward DFA that quits on non-ASCII bytes. Either a
/// precompiled dense DFA (see `prebuilt`) or a lazy DFA built at runtime.
pub enum ASet {
    Hybrid {
        dfa: HDFA,
        pool: Pool<HCache, CacheFn>,
    },
    Dense(regex_automata::dfa::dense::DFA<&'static [u32]>),
}

enum ASearch {
    Match(usize, usize),
    NoMatch,
    Quit,
}

impl ASet {
    fn build(pats: &[String]) -> Result<ASet, String> {
        let hirs = pats
            .iter()
            .map(|p| cached_hir(p))
            .collect::<Result<Vec<_>, _>>()?;
        let nfa = thompson::Compiler::new()
            .configure(
                thompson::Config::new()
                    .which_captures(WhichCaptures::None)
                    .nfa_size_limit(Some(256 << 20)),
            )
            .build_many_from_hir(&hirs)
            .map_err(|e| format!("regex crate cannot compile ASCII rule set: {e}"))?;
        let mut cfg = regex_automata::hybrid::dfa::Config::new()
            .unicode_word_boundary(true)
            .cache_capacity(8 << 20);
        for b in 0x80..=0xFFu8 {
            cfg = cfg.quit(b, true);
        }
        let dfa = HDFA::builder()
            .configure(cfg)
            .build_from_nfa(nfa)
            .map_err(|e| format!("cannot build lazy DFA: {e}"))?;
        let d2 = dfa.clone();
        let create: CacheFn = Box::new(move || d2.create_cache());
        Ok(ASet::Hybrid {
            dfa,
            pool: Pool::new(create),
        })
    }

    #[inline]
    fn search(&self, text: &str, pos: usize) -> Result<ASearch, EngineError> {
        let input = Input::new(text)
            .span(pos..text.len())
            .anchored(Anchored::Yes);
        let r = match self {
            ASet::Hybrid { dfa, pool } => {
                let mut cache = pool.get();
                dfa.try_search_fwd(&mut cache, &input)
            }
            ASet::Dense(dfa) => {
                // A DFA built with quit bytes refuses to start after a quit
                // byte (the look-behind byte decides `\b` at the start). A
                // dense DFA read back with `from_bytes` lost that check:
                // regex-automata 0.4.18 `ByteSet::from_bytes` reads both
                // halves of the quit set from the same offset, so the
                // 0x80..=0xFF half comes back empty. Do the check here.
                if pos > 0 && text.as_bytes()[pos - 1] >= 0x80 {
                    return Ok(ASearch::Quit);
                }
                use regex_automata::dfa::Automaton;
                dfa.try_search_fwd(&input)
            }
        };
        match r {
            Ok(Some(hm)) => Ok(ASearch::Match(hm.pattern().as_usize(), hm.offset())),
            Ok(None) => Ok(ASearch::NoMatch),
            Err(e) => match e.kind() {
                MatchErrorKind::Quit { .. } | MatchErrorKind::GaveUp { .. } => Ok(ASearch::Quit),
                _ => Err(EngineError(format!("DFA error: {e}"))),
            },
        }
    }
}

// ---------------------------------------------------------------------------
// compiled lexers

#[derive(Clone, Copy, Debug)]
pub enum LexRef {
    This,
    Other(usize),
}

pub enum GroupAct {
    Skip,
    Tok(TokId),
    Using { lexer: LexRef, stack: Vec<String> },
}

pub enum Action {
    None,
    Tok(TokId),
    ByGroups(Vec<GroupAct>),
    Using { lexer: LexRef, stack: Vec<String> },
}

pub enum TupleOp {
    Pop,
    PushTop,
    Push(usize),
}

pub enum Next {
    None,
    Pop(usize),
    PushTop,
    Tuple(Vec<TupleOp>),
}

pub struct Rule {
    pub src: String,
    /// full Unicode pattern (regex crate unless `fancy`)
    pub pat_n: Pat,
    /// exact word boundaries (always fancy); only for rules with \b / \B
    pub pat_d: Option<Pat>,
    /// ASCII-restricted twin of `pat_n` (only when `pat_n` is not fancy)
    pub pat_a: Option<Pat>,
    /// ASCII-restricted twin of a fancy `pat_n`, used when the whole text is ASCII
    pub pat_na: Option<Pat>,
    /// superset pattern (assertions dropped), full classes, placeholders kept
    pub pre: Option<String>,
    /// exact look-around-free rewrite of a fancy rule (see `XRule`)
    pub x: Option<XRule>,
    pub action: Action,
    pub next: Next,
}

/// A fancy rule rewritten for the regex crate: trailing positive lookaheads
/// became capture groups (`la`: the Python match ends where the first
/// participating one starts) and a leading one-character lookbehind became a
/// check on the character before the match position.
pub struct XRule {
    pub pat: Pat,
    pub pat_a: Pat,
    /// Python group g (1-based) is group `map[g - 1]` of `pat`
    pub map: Vec<usize>,
    pub la: Vec<usize>,
    pub lb: Option<(bool, Vec<(u32, u32)>)>,
}

impl XRule {
    #[inline]
    fn lookbehind_ok(&self, text: &str, pos: usize) -> bool {
        match &self.lb {
            None => true,
            Some((neg, ranges)) => {
                let prev = text[..pos].chars().next_back();
                let inside = match prev {
                    Some(c) => in_ranges(ranges, c as u32),
                    None => false,
                };
                // (?<=X): a previous char in X; (?<!X): no previous char in X
                if *neg {
                    !inside
                } else {
                    inside
                }
            }
        }
    }
}

impl Rule {
    fn needs_groups(&self) -> bool {
        matches!(self.action, Action::ByGroups(_))
    }

    /// The pattern that decides a match (fancy rules, simple engine).
    #[inline]
    fn verify_pattern(&self, exact_b: bool, all_ascii: bool) -> &Pat {
        if exact_b {
            self.pat_d.as_ref().unwrap_or(&self.pat_n)
        } else if all_ascii {
            self.pat_na.as_ref().unwrap_or(&self.pat_n)
        } else {
            &self.pat_n
        }
    }

    #[inline]
    fn pattern(&self, exact_b: bool) -> &Pat {
        if exact_b {
            self.pat_d.as_ref().unwrap_or(&self.pat_n)
        } else {
            &self.pat_n
        }
    }

    /// The exact rewrite applies unless exact word boundaries are needed and
    /// the rule has \b / \B (then only the fancy `pat_d` is exact).
    #[inline]
    fn xrule(&self, exact_b: bool) -> Option<&XRule> {
        if exact_b && self.pat_d.is_some() {
            None
        } else {
            self.x.as_ref()
        }
    }

    /// Candidate pattern text (with placeholders) for the state's rule set.
    pub fn candidate(&self, exact_b: bool) -> &str {
        if let Some(x) = self.xrule(exact_b) {
            return &x.pat.src;
        }
        let p = self.pattern(exact_b);
        if p.fancy {
            self.pre
                .as_deref()
                .expect("fancy rule carries a superset pattern")
        } else {
            &p.src
        }
    }

    /// Candidate for the ASCII tier: `pat_n` when the regex crate runs it, else
    /// the superset. In the ASCII tier `\b` is exact (all examined bytes ASCII).
    pub fn candidate_ascii(&self) -> &str {
        if let Some(x) = &self.x {
            return &x.pat_a.src;
        }
        if self.pat_n.fancy {
            self.pre
                .as_deref()
                .expect("fancy rule carries a superset pattern")
        } else {
            &self.pat_n.src
        }
    }
}

/// One lexer state: its rules plus lazily built multi-pattern sets.
/// Index `i` of each set vector covers rules `i..`.
pub struct State {
    /// "<LexerClass>/<state name>", the key of the prebuilt DFA
    pub key: String,
    pub rules: Vec<Rule>,
    aset: Vec<OnceLock<Result<ASet, String>>>,
    fset: [Vec<OnceLock<Result<meta::Regex, String>>>; 2],
}

impl State {
    fn new(key: String, rules: Vec<Rule>) -> State {
        let n = rules.len();
        fn mk<T>(n: usize) -> Vec<OnceLock<T>> {
            (0..n).map(|_| OnceLock::new()).collect()
        }
        State {
            key,
            rules,
            aset: mk(n),
            fset: [mk(n), mk(n)],
        }
    }

    /// Key of the prebuilt DFA for rules `start..`.
    pub fn set_key(&self, start: usize) -> String {
        if start == 0 {
            self.key.clone()
        } else {
            format!("{}@{}", self.key, start)
        }
    }

    /// Start indices whose ASCII-tier set can be needed: 0, and the rule after
    /// every rule whose ASCII candidate is not exact (superset or lookbehind
    /// precondition), where the search resumes when the candidate fails.
    pub fn resume_points(&self) -> Vec<usize> {
        let mut v = vec![0];
        for (j, r) in self.rules.iter().enumerate() {
            let inexact = match &r.x {
                Some(x) => x.lb.is_some(),
                None => r.pat_n.fancy,
            };
            if inexact && j + 1 < self.rules.len() {
                v.push(j + 1);
            }
        }
        v
    }

    /// ASCII-tier candidate patterns for rules `start..` (classes resolved).
    pub fn ascii_candidates(&self, start: usize) -> Vec<String> {
        self.rules[start..]
            .iter()
            .map(|r| resolve(r.candidate_ascii(), true))
            .collect()
    }

    fn aset(&self, start: usize) -> Result<&ASet, EngineError> {
        self.aset[start]
            .get_or_init(|| {
                let t0 = std::time::Instant::now();
                let pats = self.ascii_candidates(start);
                if use_prebuilt() {
                    if let Some(d) = crate::prebuilt::lookup(&self.set_key(start), &pats) {
                        if trace() {
                            eprintln!(
                                "load prebuilt {} {:.2} ms",
                                self.key,
                                t0.elapsed().as_secs_f64() * 1e3
                            );
                        }
                        return Ok(ASet::Dense(d));
                    }
                }
                let r = ASet::build(&pats);
                if trace() {
                    let len: usize = pats.iter().map(|p| p.len()).sum();
                    eprintln!(
                        "compile aset start={start} n={} len={len} {:.2} ms",
                        pats.len(),
                        t0.elapsed().as_secs_f64() * 1e3
                    );
                }
                r
            })
            .as_ref()
            .map_err(|e| EngineError(e.clone()))
    }

    fn fset(&self, exact_b: bool, start: usize) -> Result<&meta::Regex, EngineError> {
        self.fset[exact_b as usize][start]
            .get_or_init(|| {
                if trace() {
                    eprintln!("compile fset exact_b={exact_b} start={start}");
                }
                let pats: Vec<String> = self.rules[start..]
                    .iter()
                    .map(|r| resolve(r.candidate(exact_b), false))
                    .collect();
                meta::Regex::builder()
                    .configure(
                        meta::Config::new()
                            .which_captures(WhichCaptures::Implicit)
                            .nfa_size_limit(Some(256 << 20))
                            .hybrid_cache_capacity(16 << 20),
                    )
                    .build_many(&pats)
                    .map_err(|e| format!("regex crate cannot compile rule set: {e}"))
            })
            .as_ref()
            .map_err(|e| EngineError(e.clone()))
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum EngineMode {
    Tiered,
    FullOnly,
    Simple,
}

fn use_prebuilt() -> bool {
    static P: OnceLock<bool> = OnceLock::new();
    *P.get_or_init(|| std::env::var_os("RUSTGMENTS_NO_PREBUILT").is_none())
}

fn engine_mode() -> EngineMode {
    static M: OnceLock<EngineMode> = OnceLock::new();
    *M.get_or_init(|| match std::env::var("RUSTGMENTS_ENGINE").as_deref() {
        Ok("simple") => EngineMode::Simple,
        Ok("full") => EngineMode::FullOnly,
        _ => EngineMode::Tiered,
    })
}

/// First rule of `st` that matches at `pos` (Python `pattern.match(text, pos)`
/// semantics) and its match end. Fills `groups` when the rule needs them.
fn find_rule(
    st: &State,
    text: &str,
    pos: usize,
    exact_b: bool,
    all_ascii: bool,
    groups: &mut Vec<Option<(usize, usize)>>,
) -> Result<Option<(usize, usize)>, EngineError> {
    let mode = engine_mode();
    if mode == EngineMode::Simple {
        return find_rule_simple(st, text, pos, exact_b, all_ascii, groups);
    }
    let mut start = 0;
    let mut full = mode == EngineMode::FullOnly;
    while start < st.rules.len() {
        // candidate rule j with its (candidate) match end
        let (j, cand_end) = if !full {
            match st.aset(start)?.search(text, pos)? {
                ASearch::Match(p, e) => (start + p, e),
                ASearch::NoMatch => return Ok(None),
                ASearch::Quit => {
                    full = true;
                    continue;
                }
            }
        } else {
            let input = Input::new(text)
                .span(pos..text.len())
                .anchored(Anchored::Yes);
            match st.fset(exact_b, start)?.search(&input) {
                Some(m) => (start + m.pattern().as_usize(), m.end()),
                None => return Ok(None),
            }
        };
        let rule = &st.rules[j];
        // Exact rewrite of a look-around rule
        let x = if full {
            rule.xrule(exact_b)
        } else {
            rule.x.as_ref()
        };
        if let Some(x) = x {
            if !x.lookbehind_ok(text, pos) {
                start = j + 1;
                continue;
            }
            if x.la.is_empty() && !rule.needs_groups() {
                return Ok(Some((j, cand_end)));
            }
            let pat = if full { &x.pat } else { &x.pat_a };
            let mut xg = std::mem::take(groups);
            if !pat.caps_known_end(text, pos, cand_end, &mut xg)? {
                return Err(EngineError(format!(
                    "rewritten rule disagrees with its set (end {cand_end}): {}",
                    rule.src
                )));
            }
            let end =
                x.la.iter()
                    .find_map(|&g| xg.get(g).copied().flatten().map(|(s, _)| s))
                    .unwrap_or(cand_end);
            groups.clear();
            if rule.needs_groups() {
                groups.push(Some((pos, end)));
                for &g in &x.map {
                    groups.push(xg.get(g).copied().flatten());
                }
            }
            return Ok(Some((j, end)));
        }
        // Is the candidate the rule itself (exact), or a superset to verify?
        let exact = if full {
            !rule.pattern(exact_b).fancy
        } else {
            !rule.pat_n.fancy
        };
        if exact {
            if rule.needs_groups() {
                let pat = if full {
                    rule.pattern(exact_b)
                } else {
                    rule.pat_a.as_ref().unwrap()
                };
                if !pat.caps_known_end(text, pos, cand_end, groups)? {
                    return Err(EngineError(format!(
                        "rule set and single pattern disagree (end {cand_end}): {}",
                        rule.src
                    )));
                }
            }
            return Ok(Some((j, cand_end)));
        }
        let pat = rule.verify_pattern(exact_b, all_ascii);
        let r = if rule.needs_groups() {
            pat.caps_at(text, pos, groups)?
        } else {
            pat.end_at(text, pos)?
        };
        if let Some(end) = r {
            return Ok(Some((j, end)));
        }
        start = j + 1;
    }
    Ok(None)
}

/// Reference path: try every rule in order (Pygments' loop, one regex at a time).
fn find_rule_simple(
    st: &State,
    text: &str,
    pos: usize,
    exact_b: bool,
    all_ascii: bool,
    groups: &mut Vec<Option<(usize, usize)>>,
) -> Result<Option<(usize, usize)>, EngineError> {
    for (j, rule) in st.rules.iter().enumerate() {
        let pat = rule.verify_pattern(exact_b, all_ascii);
        let r = if rule.needs_groups() {
            pat.caps_at(text, pos, groups)?
        } else {
            pat.end_at(text, pos)?
        };
        if let Some(end) = r {
            return Ok(Some((j, end)));
        }
    }
    Ok(None)
}

pub struct RegexLexer {
    pub states: Vec<State>,
    pub state_names: Vec<String>,
    pub state_index: HashMap<String, usize>,
    pub root: usize,
}

pub enum Kind {
    Regex(RegexLexer),
    Json,
    /// `TextLexer`: the whole text as one `Text` token
    Text,
    Fallback,
    /// native tables that failed to build (the message says why)
    Broken(String),
}

pub fn panic_message(p: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        "panic".to_string()
    }
}

pub struct LexerEntry {
    pub class_name: String,
    pub name: String,
    pub aliases: Vec<String>,
    pub filenames: Vec<String>,
    pub backend: String,
    pub reason: Option<String>,
    states_at: Option<(usize, usize)>,
    handwritten: Option<String>,
    kind: OnceLock<Kind>,
}

impl LexerEntry {
    pub fn kind(&self, reg: &Registry) -> &Kind {
        self.kind.get_or_init(|| match self.backend.as_str() {
            "native" => {
                // A table this engine cannot build makes the lexer unusable
                // natively (callers fall back to Pygments), never the process.
                let built = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let at = self.states_at.expect("native lexer has states");
                    let states: std::collections::BTreeMap<String, Vec<RuleIr>> =
                        serde_json::from_str(ir::bulk(at)).expect("valid states");
                    build_regex_lexer(&self.class_name, &states, &reg.by_class)
                }));
                match built {
                    Ok(rl) => Kind::Regex(rl),
                    Err(p) => Kind::Broken(panic_message(p)),
                }
            }
            "handwritten" if self.handwritten.as_deref() == Some("json") => Kind::Json,
            "handwritten" if self.handwritten.as_deref() == Some("text") => Kind::Text,
            _ => Kind::Fallback,
        })
    }
}

pub struct Registry {
    pub lexers: Vec<LexerEntry>,
    pub by_class: HashMap<String, usize>,
    pub by_alias: HashMap<String, usize>,
    pub token_names: Vec<String>,
    pub token_css: Vec<String>,
    pub tok_whitespace: TokId,
    pub tok_error: TokId,
    pub tok_text: TokId,
    /// Code points where Python's `\w` and the regex crate's `\w` disagree.
    pub word_diff: Vec<(u32, u32)>,
    /// Python's `str.isspace()` set (also `\s`), for `stripall`
    pub py_space: Vec<(u32, u32)>,
    pub pygments_version: String,
    /// the variant in use: Python versions it was built with, their Unicode
    /// version, and whether it matches the interpreter that selected it
    pub variant_pythons: Vec<String>,
    pub variant_python_versions: Vec<String>,
    pub unicode_version: String,
    pub variant_ok: bool,
    pub notice: String,
}

/// The interpreter asking for tables: ("3.12", "15.0.0"). Set by
/// `select_variant` before the registry is built.
static VARIANT_REQ: OnceLock<(String, String)> = OnceLock::new();

/// Ask for the variant built for this Python version and Unicode version.
/// Returns whether the registry uses a matching variant. Only the first call
/// before the registry is built takes effect; later calls only report.
pub fn select_variant(python: &str, unicode: &str) -> bool {
    let _ = VARIANT_REQ.set((python.to_string(), unicode.to_string()));
    let req = VARIANT_REQ.get().unwrap();
    let reg = registry();
    reg.variant_ok && req.0 == python && req.1 == unicode
}

/// Index of the variant to use, and whether it matches the request. With no
/// `select_variant` call (Rust callers, tools), `RUSTGMENTS_PYTHON=3.12`
/// picks one; else the first.
fn pick_variant(variants: &[ir::VariantIr]) -> (usize, bool) {
    // Test hook: use another Python version's tables on purpose, to show that
    // the tests can tell variants apart. Never set this in real use.
    if let Ok(py) = std::env::var("RUSTGMENTS_FORCE_PYTHON") {
        if let Some(i) = variants.iter().position(|v| v.pythons.iter().any(|p| *p == py)) {
            return (i, true);
        }
    }
    if let Some((py, uni)) = VARIANT_REQ.get() {
        for (i, v) in variants.iter().enumerate() {
            if v.pythons.iter().any(|p| p == py) && v.unicode_version == *uni {
                return (i, true);
            }
        }
        return (0, false);
    }
    if let Ok(py) = std::env::var("RUSTGMENTS_PYTHON") {
        for (i, v) in variants.iter().enumerate() {
            if v.pythons.iter().any(|p| *p == py) {
                return (i, true);
            }
        }
        return (0, false);
    }
    (0, true)
}

fn rust_word_ranges() -> Vec<(u32, u32)> {
    use regex_syntax::hir::{Class, HirKind};
    let hir = regex_syntax::parse(r"\w").expect("parse \\w");
    match hir.kind() {
        HirKind::Class(Class::Unicode(c)) => c
            .ranges()
            .iter()
            .map(|r| (r.start() as u32, r.end() as u32))
            .collect(),
        _ => panic!("unexpected HIR for \\w"),
    }
}

/// Symmetric difference of two sorted, merged range lists.
fn ranges_symdiff(a: &[(u32, u32)], b: &[(u32, u32)]) -> Vec<(u32, u32)> {
    let mut events: Vec<(u32, i32)> = Vec::new();
    for &(lo, hi) in a.iter().chain(b.iter()) {
        events.push((lo, 1));
        events.push((hi + 1, -1));
    }
    events.sort();
    let mut out: Vec<(u32, u32)> = Vec::new();
    let mut depth = 0;
    let mut i = 0;
    let mut cur_start: Option<u32> = None;
    while i < events.len() {
        let p = events[i].0;
        while i < events.len() && events[i].0 == p {
            depth += events[i].1;
            i += 1;
        }
        let odd = depth == 1;
        match (cur_start, odd) {
            (None, true) => cur_start = Some(p),
            (Some(s), false) => {
                out.push((s, p - 1));
                cur_start = None;
            }
            _ => {}
        }
    }
    out
}

impl Registry {
    pub fn build() -> Registry {
        let ir = ir::load();
        let token_names: Vec<String> = ir.token_types.iter().map(|t| t.name.clone()).collect();
        let token_css: Vec<String> = ir.token_types.iter().map(|t| t.css.clone()).collect();
        let find_tok = |n: &str| -> TokId {
            token_names
                .iter()
                .position(|x| x == n)
                .unwrap_or_else(|| panic!("token {n} missing")) as TokId
        };
        let tok_whitespace = find_tok("Token.Text.Whitespace");
        let tok_error = find_tok("Token.Error");
        let tok_text = find_tok("Token.Text");
        let (vi, variant_ok) = pick_variant(&ir.variants);
        let variant = &ir.variants[vi];
        let mut full: Vec<Option<String>> = Vec::with_capacity(ir.classes.len());
        let mut ascii: Vec<Option<String>> = Vec::with_capacity(ir.classes.len());
        for c in &ir.classes {
            full.push(c.as_ref().map(|c| c.0.clone()));
            ascii.push(c.as_ref().map(|c| c.1.clone()));
        }
        let vclasses: Vec<(usize, String, String)> =
            serde_json::from_str(ir::bulk(variant.classes_at)).expect("valid variant classes");
        for (i, f, a) in vclasses {
            full[i] = Some(f);
            ascii[i] = Some(a);
        }
        let text = |v: Vec<Option<String>>| -> Vec<String> {
            v.into_iter()
                .map(|c| c.expect("every symbolic class has text in the variant"))
                .collect()
        };
        let _ = CLASSES.set(Classes {
            full: text(full),
            ascii: text(ascii),
        });
        let mut by_class = HashMap::new();
        for (i, n) in ir.lexers.keys().enumerate() {
            by_class.insert(n.clone(), i);
        }
        let mut lexers = Vec::new();
        let mut by_alias = HashMap::new();
        for (idx, (cname, lx)) in ir.lexers.into_iter().enumerate() {
            for a in &lx.aliases {
                by_alias.insert(a.clone(), idx);
            }
            lexers.push(LexerEntry {
                class_name: cname,
                name: lx.name,
                aliases: lx.aliases,
                filenames: lx.filenames,
                backend: lx.backend,
                reason: lx.reason,
                states_at: lx.states_at,
                handwritten: lx.handwritten,
                kind: OnceLock::new(),
            });
        }
        let word_diff = ranges_symdiff(&variant.py_word, &rust_word_ranges());
        let py_space = variant.py_space.clone();
        let variant_pythons = variant.pythons.clone();
        let variant_python_versions = variant.python_versions.clone();
        let unicode_version = variant.unicode_version.clone();
        Registry {
            lexers,
            by_class,
            by_alias,
            token_names,
            token_css,
            tok_whitespace,
            tok_error,
            tok_text,
            word_diff,
            py_space,
            pygments_version: ir.pygments_version,
            variant_pythons,
            variant_python_versions,
            unicode_version,
            variant_ok,
            notice: ir.notice,
        }
    }

    /// True when the text contains a character on which Python's and the
    /// regex crate's word classes disagree; then `\b` needs the exact form.
    pub fn needs_exact_boundaries(&self, text: &str) -> bool {
        if text.is_ascii() {
            return false;
        }
        text.chars().any(|c| {
            let cp = c as u32;
            cp >= 0x80 && in_ranges(&self.word_diff, cp)
        })
    }
}

/// Code point ranges of a one-character class text (`a`, `\x{2E}`, `[...]`).
fn class_ranges(text: &str) -> Vec<(u32, u32)> {
    use regex_syntax::hir::{Class, HirKind};
    let hir = regex_syntax::ParserBuilder::new()
        .build()
        .parse(text)
        .unwrap_or_else(|e| panic!("bad one-character class {text:?}: {e}"));
    match hir.kind() {
        HirKind::Class(Class::Unicode(c)) => c
            .ranges()
            .iter()
            .map(|r| (r.start() as u32, r.end() as u32))
            .collect(),
        HirKind::Literal(lit) => {
            let s = std::str::from_utf8(&lit.0).expect("literal is UTF-8");
            let mut it = s.chars();
            let c = it.next().expect("one character");
            assert!(it.next().is_none(), "one-character class {text:?}");
            vec![(c as u32, c as u32)]
        }
        _ => panic!("unexpected one-character class {text:?}"),
    }
}

pub fn in_ranges(r: &[(u32, u32)], cp: u32) -> bool {
    r.binary_search_by(|&(lo, hi)| {
        if hi < cp {
            std::cmp::Ordering::Less
        } else if lo > cp {
            std::cmp::Ordering::Greater
        } else {
            std::cmp::Ordering::Equal
        }
    })
    .is_ok()
}

fn build_regex_lexer(
    class_name: &str,
    states_ir: &std::collections::BTreeMap<String, Vec<RuleIr>>,
    by_class: &HashMap<String, usize>,
) -> RegexLexer {
    let state_names: Vec<String> = states_ir.keys().cloned().collect();
    let mut state_index = HashMap::new();
    for (i, n) in state_names.iter().enumerate() {
        state_index.insert(n.clone(), i);
    }
    let lexref = |name: &str| -> LexRef {
        if name == "this" {
            LexRef::This
        } else {
            LexRef::Other(*by_class.get(name).expect("using target present in IR"))
        }
    };
    let conv_stack = |s: &Option<Vec<String>>| -> Vec<String> {
        s.clone().unwrap_or_else(|| vec!["root".to_string()])
    };
    let mut states = Vec::new();
    for name in &state_names {
        let mut rules = Vec::new();
        for r in &states_ir[name] {
            let action = match &r.action {
                None => Action::None,
                Some(ActionIr::Tok { t }) => Action::Tok(*t),
                Some(ActionIr::Using { lexer, stack }) => Action::Using {
                    lexer: lexref(lexer),
                    stack: conv_stack(stack),
                },
                Some(ActionIr::ByGroups { g }) => Action::ByGroups(
                    g.iter()
                        .map(|a| match a {
                            None => GroupAct::Skip,
                            Some(ActionIr::Tok { t }) => GroupAct::Tok(*t),
                            Some(ActionIr::Using { lexer, stack }) => GroupAct::Using {
                                lexer: lexref(lexer),
                                stack: conv_stack(stack),
                            },
                            Some(ActionIr::ByGroups { .. }) => {
                                unreachable!("rejected by extractor")
                            }
                        })
                        .collect(),
                ),
            };
            let next = match &r.next {
                None => Next::None,
                Some(NextIr::Pop { n }) => Next::Pop(*n),
                Some(NextIr::PushTop) => Next::PushTop,
                Some(NextIr::Tuple { items }) => Next::Tuple(
                    items
                        .iter()
                        .map(|s| match s.as_str() {
                            "#pop" => TupleOp::Pop,
                            "#push" => TupleOp::PushTop,
                            other => TupleOp::Push(state_index[other]),
                        })
                        .collect(),
                ),
            };
            let pat_n = Pat::new(r.re.clone(), r.fancy, false);
            let pat_a = if r.fancy {
                None
            } else {
                Some(Pat::new(r.re.clone(), false, true))
            };
            let pat_na = if r.fancy {
                Some(Pat::new(r.re.clone(), true, true))
            } else {
                None
            };
            let pat_d = r.re_d.as_ref().map(|s| Pat::new(s.clone(), true, false));
            let x = r.rx.as_ref().map(|rx| XRule {
                pat: Pat::new(rx.clone(), false, false),
                pat_a: Pat::new(rx.clone(), false, true),
                map: r.rx_map.clone().unwrap_or_default(),
                la: r.rx_la.clone().unwrap_or_default(),
                lb: r
                    .rx_lb
                    .as_ref()
                    .map(|lb| (lb.neg, class_ranges(&resolve(&lb.set, false)))),
            });
            rules.push(Rule {
                src: r.src.clone(),
                pat_n,
                pat_d,
                pat_a,
                pat_na,
                pre: r.pre.clone(),
                x,
                action,
                next,
            });
        }
        states.push(State::new(format!("{class_name}/{name}"), rules));
    }
    let root = state_index["root"];
    RegexLexer {
        states,
        state_names,
        state_index,
        root,
    }
}

static REGISTRY: OnceLock<Registry> = OnceLock::new();

pub fn registry() -> &'static Registry {
    REGISTRY.get_or_init(Registry::build)
}

// ---------------------------------------------------------------------------
// lexing

pub struct LexCtx<'r> {
    pub reg: &'r Registry,
    pub exact_b: bool,
    pub all_ascii: bool,
    groups: Vec<Option<(usize, usize)>>,
}

impl<'r> LexCtx<'r> {
    pub fn new(reg: &'r Registry, text: &str) -> LexCtx<'r> {
        let all_ascii = text.is_ascii();
        LexCtx {
            reg,
            exact_b: !all_ascii && reg.needs_exact_boundaries(text),
            all_ascii,
            groups: Vec::new(),
        }
    }
}

/// `get_tokens_unprocessed(text)` for any lexer in the registry, appending to
/// `out` with offsets shifted by `base`.
pub fn lex(
    ctx: &mut LexCtx,
    lexer: usize,
    text: &str,
    base: usize,
    stack: Option<&[String]>,
    out: &mut Vec<Tok>,
) -> Result<(), EngineError> {
    let reg = ctx.reg;
    match reg.lexers[lexer].kind(reg) {
        Kind::Regex(rl) => lex_regex(ctx, lexer, rl, text, base, stack, out),
        Kind::Text => {
            // TextLexer.get_tokens_unprocessed: `yield 0, Text, text`
            out.push(Tok {
                start: base,
                end: base + text.len(),
                tt: reg.tok_text,
            });
            Ok(())
        }
        Kind::Json => {
            crate::json::lex_json(reg, text, base, out);
            Ok(())
        }
        Kind::Fallback => Err(EngineError(format!(
            "lexer {} is fallback-only",
            reg.lexers[lexer].class_name
        ))),
        Kind::Broken(why) => Err(EngineError(format!(
            "lexer {} tables failed to build: {why}",
            reg.lexers[lexer].class_name
        ))),
    }
}

fn lex_regex(
    ctx: &mut LexCtx,
    lexer_idx: usize,
    rl: &RegexLexer,
    text: &str,
    base: usize,
    stack_names: Option<&[String]>,
    out: &mut Vec<Tok>,
) -> Result<(), EngineError> {
    let reg = ctx.reg;
    let mut stack: Vec<usize> = match stack_names {
        None => vec![rl.root],
        Some(names) => names
            .iter()
            .map(|n| {
                rl.state_index
                    .get(n)
                    .copied()
                    .ok_or_else(|| EngineError(format!("unknown state {n}")))
            })
            .collect::<Result<_, _>>()?,
    };
    let mut pos = 0usize;
    let mut cur = *stack.last().unwrap();
    let mut groups = std::mem::take(&mut ctx.groups);
    loop {
        let st = &rl.states[cur];
        if let Some((j, end)) = find_rule(st, text, pos, ctx.exact_b, ctx.all_ascii, &mut groups)? {
            let rule = &st.rules[j];
            match &rule.action {
                Action::None => {}
                Action::Tok(t) => out.push(Tok {
                    start: base + pos,
                    end: base + end,
                    tt: *t,
                }),
                Action::ByGroups(acts) => {
                    let spans: Vec<Option<(usize, usize)>> = groups.clone();
                    for (i, act) in acts.iter().enumerate() {
                        let g = spans.get(i + 1).copied().ok_or_else(|| {
                            EngineError(format!("bygroups refers to missing group {}", i + 1))
                        })?;
                        match act {
                            GroupAct::Skip => {}
                            GroupAct::Tok(t) => {
                                if let Some((s, e)) = g {
                                    if e > s {
                                        out.push(Tok {
                                            start: base + s,
                                            end: base + e,
                                            tt: *t,
                                        });
                                    }
                                }
                            }
                            GroupAct::Using { lexer, stack } => {
                                if let Some((s, e)) = g {
                                    let target = match lexer {
                                        LexRef::This => lexer_idx,
                                        LexRef::Other(i) => *i,
                                    };
                                    ctx.groups = std::mem::take(&mut groups);
                                    lex(ctx, target, &text[s..e], base + s, Some(stack), out)?;
                                    groups = std::mem::take(&mut ctx.groups);
                                }
                            }
                        }
                    }
                }
                Action::Using { lexer, stack } => {
                    let target = match lexer {
                        LexRef::This => lexer_idx,
                        LexRef::Other(i) => *i,
                    };
                    ctx.groups = std::mem::take(&mut groups);
                    lex(ctx, target, &text[pos..end], base + pos, Some(stack), out)?;
                    groups = std::mem::take(&mut ctx.groups);
                }
            }
            pos = end;
            match &rule.next {
                Next::None => {}
                Next::Pop(n) => {
                    if *n >= stack.len() {
                        stack.truncate(1);
                    } else {
                        let l = stack.len() - n;
                        stack.truncate(l);
                    }
                    cur = *stack.last().unwrap();
                }
                Next::PushTop => {
                    let top = *stack.last().unwrap();
                    stack.push(top);
                    cur = top;
                }
                Next::Tuple(ops) => {
                    for op in ops {
                        match op {
                            TupleOp::Pop => {
                                if stack.len() > 1 {
                                    stack.pop();
                                }
                            }
                            TupleOp::PushTop => {
                                let top = *stack.last().unwrap();
                                stack.push(top);
                            }
                            TupleOp::Push(s) => stack.push(*s),
                        }
                    }
                    cur = *stack.last().unwrap();
                }
            }
            continue;
        }
        // no rule matched
        if pos >= text.len() {
            break;
        }
        let ch = text[pos..].chars().next().unwrap();
        let clen = ch.len_utf8();
        if ch == '\n' {
            stack.clear();
            stack.push(rl.root);
            cur = rl.root;
            out.push(Tok {
                start: base + pos,
                end: base + pos + 1,
                tt: reg.tok_whitespace,
            });
        } else {
            out.push(Tok {
                start: base + pos,
                end: base + pos + clen,
                tt: reg.tok_error,
            });
        }
        pos += clen;
    }
    ctx.groups = groups;
    Ok(())
}

// ---------------------------------------------------------------------------
// preprocessing (Lexer._preprocess_lexer_input with default options)

pub fn preprocess(text: &str) -> String {
    let t = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut s = if t.contains('\r') {
        t.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        t.to_string()
    };
    let trimmed = s.trim_matches('\n');
    if trimmed.len() != s.len() {
        s = trimmed.to_string();
    }
    if !s.ends_with('\n') {
        s.push('\n');
    }
    s
}
