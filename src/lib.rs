//! rustgments: a Rust engine that reproduces Pygments 2.21.0 output for its
//! declarative RegexLexers, driven by tables extracted from Pygments itself.
//!
//! Pygments is BSD-2-Clause; its notice travels with the generated IR
//! (`ir/pygments_ir.json`, field `notice`) and in `LICENSE-pygments`.

pub mod engine;
pub mod html;
pub mod ir;
pub mod json;
pub mod prebuilt;

use engine::{registry, EngineError, LexCtx, Tok};

/// Lex `text` (already preprocessed) with the lexer whose class name is given.
pub fn tokens_unprocessed(class_name: &str, text: &str) -> Result<Vec<Tok>, EngineError> {
    let reg = registry();
    let idx = *reg
        .by_class
        .get(class_name)
        .ok_or_else(|| EngineError(format!("unknown lexer {class_name}")))?;
    // An engine bug must cost a fallback to Pygments, not the process.
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut ctx = LexCtx::new(reg, text);
        let mut out = Vec::with_capacity(text.len() / 4 + 16);
        engine::lex(&mut ctx, idx, text, 0, None, &mut out)?;
        Ok(out)
    }))
    .unwrap_or_else(|p| Err(EngineError(format!("engine panic: {}", engine::panic_message(p)))))
}

/// `highlight(code, Lexer(), HtmlFormatter())` for a native lexer.
pub fn highlight_html(class_name: &str, code: &str) -> Result<String, EngineError> {
    let text = engine::preprocess(code);
    let toks = tokens_unprocessed(class_name, &text)?;
    Ok(html::format_html(spans(), &text, &toks))
}

fn spans() -> &'static html::Spans {
    static SPANS: std::sync::OnceLock<html::Spans> = std::sync::OnceLock::new();
    SPANS.get_or_init(|| html::Spans::new(registry()))
}

#[cfg(feature = "python")]
mod py {
    use super::{engine, html, registry, tokens_unprocessed, EngineError, Tok};
    use pyo3::create_exception;
    use pyo3::exceptions::{PyRuntimeError, PyValueError};
    use pyo3::prelude::*;
    use pyo3::pybacked::PyBackedStr;
    use pyo3::sync::GILOnceCell;
    use pyo3::types::{PyList, PyString, PyTuple};
    use std::sync::{Arc, Mutex};

    create_exception!(_native, EngineFailure, PyRuntimeError);

    fn err(e: EngineError) -> PyErr {
        EngineFailure::new_err(e.0)
    }

    /// Pygments token type objects, indexed by token id (set by the Python
    /// layer once, so that token tuples carry the real objects).
    static TTYPES: GILOnceCell<Vec<PyObject>> = GILOnceCell::new();

    fn ttypes(py: Python<'_>) -> PyResult<&Vec<PyObject>> {
        TTYPES
            .get(py)
            .ok_or_else(|| PyRuntimeError::new_err("set_token_types() was not called"))
    }

    #[pyfunction]
    fn set_token_types(py: Python<'_>, types: Vec<PyObject>) -> PyResult<()> {
        if types.len() != registry().token_names.len() {
            return Err(PyValueError::new_err("one token type per token id expected"));
        }
        let _ = TTYPES.set(py, types);
        Ok(())
    }

    #[pyfunction]
    fn info(py: Python<'_>) -> PyResult<PyObject> {
        let reg = registry();
        let d = pyo3::types::PyDict::new(py);
        d.set_item("pygments_version", &reg.pygments_version)?;
        d.set_item("variant_pythons", reg.variant_pythons.clone())?;
        d.set_item("variant_python_versions", reg.variant_python_versions.clone())?;
        d.set_item("unicode_version", &reg.unicode_version)?;
        d.set_item("variant_ok", reg.variant_ok)?;
        d.set_item("notice", &reg.notice)?;
        Ok(d.into_any().unbind())
    }

    /// Pick the tables built for this interpreter ("3.12", unicodedata's
    /// version). Call before anything else; returns whether such tables exist.
    #[pyfunction]
    fn select_variant(python: &str, unicode: &str) -> bool {
        engine::select_variant(python, unicode)
    }

    #[pyfunction]
    fn lexers(py: Python<'_>) -> PyResult<PyObject> {
        let reg = registry();
        let l = PyList::empty(py);
        for e in &reg.lexers {
            let t = PyTuple::new(
                py,
                [
                    e.class_name.clone().into_pyobject(py)?.into_any(),
                    e.name.clone().into_pyobject(py)?.into_any(),
                    e.aliases.clone().into_pyobject(py)?.into_any(),
                    e.filenames.clone().into_pyobject(py)?.into_any(),
                    e.backend.clone().into_pyobject(py)?.into_any(),
                    e.reason.clone().into_pyobject(py)?.into_any(),
                ],
            )?;
            l.append(t)?;
        }
        Ok(l.into_any().unbind())
    }

    #[pyfunction]
    fn token_names() -> Vec<String> {
        registry().token_names.clone()
    }

    #[pyfunction]
    fn preprocess(text: PyBackedStr) -> String {
        engine::preprocess(&text)
    }

    fn lex(py: Python<'_>, class_name: &str, s: &str) -> PyResult<Vec<Tok>> {
        py.allow_threads(|| tokens_unprocessed(class_name, s))
            .map_err(err)
    }

    /// Code point offsets of the token starts (Python string indices).
    fn cp_offsets(s: &str, toks: &[Tok]) -> Vec<usize> {
        if s.is_ascii() {
            return toks.iter().map(|t| t.start).collect();
        }
        let mut out = Vec::with_capacity(toks.len());
        let (mut last_b, mut last_c) = (0usize, 0usize);
        for t in toks {
            if t.start < last_b {
                last_b = 0;
                last_c = 0;
            }
            last_c += s[last_b..t.start].chars().count();
            last_b = t.start;
            out.push(last_c);
        }
        out
    }

    /// List of (codepoint_offset, token_id, value) like Pygments'
    /// get_tokens_unprocessed. `text` is used as given (no preprocessing).
    #[pyfunction]
    fn get_tokens_unprocessed(
        py: Python<'_>,
        class_name: &str,
        text: PyBackedStr,
    ) -> PyResult<PyObject> {
        let s: &str = &text;
        let toks = lex(py, class_name, s)?;
        let offs = cp_offsets(s, &toks);
        let l = PyList::empty(py);
        for (t, cp) in toks.iter().zip(offs) {
            let tup = PyTuple::new(
                py,
                [
                    cp.into_pyobject(py)?.into_any(),
                    (t.tt as usize).into_pyobject(py)?.into_any(),
                    PyString::new(py, &s[t.start..t.end]).into_any(),
                ],
            )?;
            l.append(tup)?;
        }
        Ok(l.into_any().unbind())
    }

    /// Like `get_tokens_unprocessed`, with the Pygments token type objects
    /// (see `set_token_types`) instead of ids.
    #[pyfunction]
    fn tokens_with_offsets(
        py: Python<'_>,
        class_name: &str,
        text: PyBackedStr,
    ) -> PyResult<PyObject> {
        let s: &str = &text;
        let toks = lex(py, class_name, s)?;
        let tt = ttypes(py)?;
        let offs = cp_offsets(s, &toks);
        let l = PyList::empty(py);
        for (t, cp) in toks.iter().zip(offs) {
            let tup = PyTuple::new(
                py,
                [
                    cp.into_pyobject(py)?.into_any(),
                    tt[t.tt as usize].bind(py).clone(),
                    PyString::new(py, &s[t.start..t.end]).into_any(),
                ],
            )?;
            l.append(tup)?;
        }
        Ok(l.into_any().unbind())
    }

    /// (token type, value) pairs, like the unfiltered `Lexer.get_tokens`
    /// stream for already preprocessed text.
    #[pyfunction]
    fn tokens(py: Python<'_>, class_name: &str, text: PyBackedStr) -> PyResult<PyObject> {
        let s: &str = &text;
        let toks = lex(py, class_name, s)?;
        let tt = ttypes(py)?;
        let l = PyList::empty(py);
        for t in &toks {
            let tup = PyTuple::new(
                py,
                [
                    tt[t.tt as usize].bind(py).clone(),
                    PyString::new(py, &s[t.start..t.end]).into_any(),
                ],
            )?;
            l.append(tup)?;
        }
        Ok(l.into_any().unbind())
    }

    #[pyfunction]
    fn highlight_html(py: Python<'_>, class_name: &str, code: PyBackedStr) -> PyResult<String> {
        let s: &str = &code;
        py.allow_threads(|| super::highlight_html(class_name, s))
            .map_err(err)
    }

    /// Registered span opener tables (see `register_openers`).
    static OPENERS: Mutex<Vec<Arc<html::Spans>>> = Mutex::new(Vec::new());

    /// Register the span opener of every token id and the line separator of
    /// one formatter configuration; returns a handle for `format_lines`.
    #[pyfunction]
    fn register_openers(openers: Vec<String>, lsep: String) -> PyResult<usize> {
        if openers.len() != registry().token_names.len() {
            return Err(PyValueError::new_err("one opener per token id expected"));
        }
        let mut v = OPENERS.lock().unwrap();
        v.push(Arc::new(html::Spans::from_openers(&openers, &lsep)));
        Ok(v.len() - 1)
    }

    /// `HtmlFormatter._format_lines` over the native tokens of `text`
    /// (already preprocessed): the list of formatted lines. With
    /// `check_error`, returns None if any token is `Token.Error` (the caller
    /// then takes the Pygments path, where `raiseonerror` raises in place).
    #[pyfunction]
    fn format_lines(
        py: Python<'_>,
        class_name: &str,
        text: PyBackedStr,
        handle: usize,
        check_error: bool,
    ) -> PyResult<Option<Vec<String>>> {
        let s: &str = &text;
        let spans = OPENERS
            .lock()
            .unwrap()
            .get(handle)
            .cloned()
            .ok_or_else(|| PyValueError::new_err("unknown opener handle"))?;
        py.allow_threads(|| {
            let toks = tokens_unprocessed(class_name, s)?;
            if check_error {
                let e = registry().tok_error;
                if toks.iter().any(|t| t.tt == e) {
                    return Ok(None);
                }
            }
            let mut lines = Vec::new();
            html::format_lines(&spans, s, &toks, |l| lines.push(l.to_string()));
            Ok(Some(lines))
        })
        .map_err(err)
    }

    /// Lex only (preprocess + tokens), returns the token count. For benchmarks.
    #[pyfunction]
    fn lex_count(py: Python<'_>, class_name: &str, code: PyBackedStr) -> PyResult<usize> {
        let s: &str = &code;
        py.allow_threads(|| {
            let text = engine::preprocess(s);
            tokens_unprocessed(class_name, &text).map(|t| t.len())
        })
        .map_err(err)
    }

    #[pymodule]
    fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(info, m)?)?;
        m.add_function(wrap_pyfunction!(select_variant, m)?)?;
        m.add_function(wrap_pyfunction!(set_token_types, m)?)?;
        m.add_function(wrap_pyfunction!(lexers, m)?)?;
        m.add_function(wrap_pyfunction!(token_names, m)?)?;
        m.add_function(wrap_pyfunction!(preprocess, m)?)?;
        m.add_function(wrap_pyfunction!(get_tokens_unprocessed, m)?)?;
        m.add_function(wrap_pyfunction!(tokens_with_offsets, m)?)?;
        m.add_function(wrap_pyfunction!(tokens, m)?)?;
        m.add_function(wrap_pyfunction!(highlight_html, m)?)?;
        m.add_function(wrap_pyfunction!(register_openers, m)?)?;
        m.add_function(wrap_pyfunction!(format_lines, m)?)?;
        m.add_function(wrap_pyfunction!(lex_count, m)?)?;
        m.add("EngineFailure", m.py().get_type::<EngineFailure>())?;
        Ok(())
    }
}
