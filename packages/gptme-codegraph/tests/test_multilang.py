"""Tests for multi-language support in gptme-codegraph (JS/TS, Rust, Go, Java, C#, Ruby, C++, Kotlin, Swift).

Verifies that parse_file, extract_symbols, and build_index work correctly
for JavaScript, TypeScript, Rust, Go, Java, C#, Ruby, C++, Kotlin, and Swift
source files.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from gptme_codegraph.core import (
    _tree_sitter_parse,
    build_call_graph,
    build_index,
    extract_symbols,
    parse_file,
)


def _has_grammar(lang: str) -> bool:
    """Check if a tree-sitter grammar is available."""
    import importlib

    try:
        importlib.import_module(f"tree_sitter_{lang}")
        return True
    except ImportError:
        return False


_skip_no_js = pytest.mark.skipif(
    not _has_grammar("javascript"),
    reason="tree-sitter-javascript not installed",
)
_skip_no_ts = pytest.mark.skipif(
    not _has_grammar("typescript"),
    reason="tree-sitter-typescript not installed",
)
_skip_no_rust = pytest.mark.skipif(
    not _has_grammar("rust"),
    reason="tree-sitter-rust not installed",
)
_skip_no_java = pytest.mark.skipif(
    not _has_grammar("java"),
    reason="tree-sitter-java not installed",
)
_skip_no_csharp = pytest.mark.skipif(
    not _has_grammar("c_sharp"),
    reason="tree-sitter-c-sharp not installed",
)
_skip_no_cpp = pytest.mark.skipif(
    not _has_grammar("cpp"),
    reason="tree-sitter-cpp not installed",
)
_skip_no_ruby = pytest.mark.skipif(
    not _has_grammar("ruby"),
    reason="tree-sitter-ruby not installed",
)
_skip_no_php = pytest.mark.skipif(
    not _has_grammar("php"),
    reason="tree-sitter-php not installed",
)
_skip_no_kotlin = pytest.mark.skipif(
    not _has_grammar("kotlin"),
    reason="tree-sitter-kotlin not installed",
)
_skip_no_swift = pytest.mark.skipif(
    not _has_grammar("swift"),
    reason="tree-sitter-swift not installed",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def js_module() -> Generator[Path, None, None]:
    """A JavaScript module with functions, classes, and methods."""
    code = """\
// utils.js — shared helpers

export function greet(name) {
    return `Hello ${name}`;
}

export function formatDate(date) {
    const ts = date.getTime();
    return date.toISOString().split('T')[0];
}

export class Calculator {
    constructor(initialValue = 0) {
        this.value = initialValue;
    }

    add(n) {
        this.value += n;
        return this;
    }

    multiply(n) {
        this.value *= n;
        return this;
    }

    result() {
        return this.value;
    }
}

const PI = 3.14159;
export { PI };
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, prefix="test_js_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def ts_module() -> Generator[Path, None, None]:
    """A TypeScript module with functions, classes, and methods."""
    code = """\
// models.ts — typed data models

export interface User {
    id: number;
    name: string;
    email: string;
}

export class UserRepository {
    private users: User[] = [];

    addUser(user: User): void {
        this.users.push(user);
    }

    findById(id: number): User | undefined {
        return this.users.find(u => u.id === id);
    }

    findAll(): User[] {
        return [...this.users];
    }
}

export function validateEmail(email: string): boolean {
    const re = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;
    return re.test(email);
}

export const APP_VERSION = "1.0.0";
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ts", delete=False, prefix="test_ts_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def tsx_module() -> Generator[Path, None, None]:
    """A TSX module with React components."""
    code = """\
// Button.tsx — reusable button component
import React from 'react';

interface ButtonProps {
    label: string;
    onClick: () => void;
    disabled?: boolean;
}

export function Button({ label, onClick, disabled = false }: ButtonProps): JSX.Element {
    return (
        <button onClick={onClick} disabled={disabled} className="btn">
            {label}
        </button>
    );
}

export function IconButton({ icon, ...props }: ButtonProps & { icon: string }): JSX.Element {
    return (
        <button {...props} className="btn-icon">
            <span className="icon">{icon}</span>
            <span>{props.label}</span>
        </button>
    );
}
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsx", delete=False, prefix="test_tsx_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def rust_module() -> Generator[Path, None, None]:
    """A Rust module with functions, struct, impl, enum, and trait."""
    code = """\
// calculator.rs — a simple calculator module

/// Adds two numbers together.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Multiplies two numbers.
pub fn multiply(a: i32, b: i32) -> i32 {
    let result = a * b;
    multiply_internal(result)
}

fn multiply_internal(value: i32) -> i32 {
    value
}

pub struct Config {
    pub precision: u8,
    pub max_value: i32,
}

impl Config {
    pub fn new(precision: u8) -> Self {
        Self {
            precision,
            max_value: 100,
        }
    }

    pub fn with_max(mut self, max: i32) -> Self {
        self.max_value = max;
        self
    }
}

pub enum Operation {
    Add,
    Subtract,
    Multiply,
    Divide,
}

pub trait Calculator {
    fn calculate(&self, op: Operation, a: i32, b: i32) -> i32;
}

use std::fmt;
use std::io::{self, Write};
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rs", delete=False, prefix="test_rust_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def rust_simple() -> Generator[Path, None, None]:
    """A minimal Rust file for quick parse tests."""
    code = """\
fn main() {
    println!("Hello");
}
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rs", delete=False, prefix="test_rust_simple_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def multilang_project(tmp_path: Path) -> Generator[Path, None, None]:
    """A project directory with JS, TS, and Rust source files."""
    proj = tmp_path / "multilang"
    proj.mkdir()

    js_dir = proj / "src" / "js"
    js_dir.mkdir(parents=True)
    (js_dir / "main.js").write_text(
        "export function run() { return 42; }\n"
        "export class App { start() { run(); } }\n"
    )

    ts_dir = proj / "src" / "ts"
    ts_dir.mkdir(parents=True)
    (ts_dir / "types.ts").write_text(
        "export interface Config { debug: boolean; }\n"
        "export function loadConfig(): Config { return { debug: true }; }\n"
    )

    rust_dir = proj / "src" / "rust"
    rust_dir.mkdir(parents=True)
    (rust_dir / "lib.rs").write_text(
        "pub fn init() -> bool { true }\npub struct State { pub ready: bool }\n"
    )

    yield proj


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_language_detection_python():
    """Default language is python."""
    code = b"def foo(): pass\n"
    root, parser = _tree_sitter_parse(code)
    assert root is not None
    assert root.type == "module"


@_skip_no_js
def test_language_detection_js():
    """_tree_sitter_parse detects .js files."""
    code = b"function foo() { return 1; }\n"
    root, parser = _tree_sitter_parse(code, lang_name="javascript")
    assert root is not None
    assert root.type == "program"


@_skip_no_ts
def test_language_detection_ts():
    """_tree_sitter_parse detects .ts files."""
    code = b"function foo(): number { return 1; }\n"
    root, parser = _tree_sitter_parse(code, lang_name="typescript")
    assert root is not None
    assert root.type == "program"


@_skip_no_ts
def test_language_detection_tsx():
    """_tree_sitter_parse detects .tsx files."""
    code = b"const el = <div>hello</div>;\n"
    root, parser = _tree_sitter_parse(code, lang_name="tsx")
    assert root is not None
    assert root.type == "program"


@_skip_no_rust
def test_language_detection_rust():
    """_tree_sitter_parse detects .rs files."""
    code = b"fn main() {}\n"
    root, parser = _tree_sitter_parse(code, lang_name="rust")
    assert root is not None
    assert root.type == "source_file"


# ---------------------------------------------------------------------------
# JavaScript parsing
# ---------------------------------------------------------------------------


@_skip_no_js
def test_parse_js_functions(js_module: Path):
    """JavaScript: parse_file extracts functions."""
    result = parse_file(js_module)
    func_names = {s.name for s in result.symbols if s.kind == "function"}
    assert "greet" in func_names
    assert "formatDate" in func_names


@_skip_no_js
def test_parse_js_classes(js_module: Path):
    """JavaScript: parse_file extracts classes and methods."""
    result = parse_file(js_module)
    class_names = {s.name for s in result.symbols if s.kind == "class"}
    assert "Calculator" in class_names

    methods = [s for s in result.symbols if s.kind == "method"]
    method_names = {s.name for s in methods}
    assert "add" in method_names
    assert "multiply" in method_names
    assert "result" in method_names
    # Verify parent class linkage
    for m in methods:
        assert m.parent_class == "Calculator"


@_skip_no_js
def test_parse_js_method_locations(js_module: Path):
    """JavaScript: method locations are correct."""
    result = parse_file(js_module)
    constructor = [s for s in result.symbols if s.name == "constructor"]
    assert len(constructor) == 1
    ctor = constructor[0]
    assert ctor.kind == "method"
    assert ctor.parent_class == "Calculator"
    assert ctor.start_line >= 1
    assert ctor.end_line >= ctor.start_line


@_skip_no_js
def test_extract_symbols_js(js_module: Path):
    """extract_symbols works on JS files."""
    symbols = extract_symbols(js_module)
    assert len(symbols) >= 5  # 2 functions + 1 class + 3 methods + 1 variable
    assert any(s.name == "greet" and s.kind == "function" for s in symbols)


# ---------------------------------------------------------------------------
# TypeScript parsing
# ---------------------------------------------------------------------------


@_skip_no_ts
def test_parse_ts_functions(ts_module: Path):
    """TypeScript: parse_file extracts functions."""
    result = parse_file(ts_module)
    func_names = {s.name for s in result.symbols if s.kind == "function"}
    assert "validateEmail" in func_names


@_skip_no_ts
def test_parse_ts_classes(ts_module: Path):
    """TypeScript: parse_file extracts classes and methods."""
    result = parse_file(ts_module)
    class_names = {s.name for s in result.symbols if s.kind == "class"}
    assert "UserRepository" in class_names

    methods = [s for s in result.symbols if s.kind == "method"]
    method_names = {s.name for s in methods}
    assert "addUser" in method_names
    assert "findById" in method_names
    assert "findAll" in method_names


@_skip_no_ts
def test_parse_ts_interface_not_symbol(ts_module: Path):
    """TypeScript: interfaces are not extracted as symbols."""
    result = parse_file(ts_module)
    # Interfaces are not classes/functions/methods, so they shouldn't appear
    interface_symbols = [s for s in result.symbols if s.name == "User"]
    assert len(interface_symbols) == 0


@_skip_no_ts
def test_parse_tsx_react_components(tsx_module: Path):
    """TSX: React function components are extracted."""
    result = parse_file(tsx_module)
    func_names = {s.name for s in result.symbols if s.kind == "function"}
    assert "Button" in func_names
    assert "IconButton" in func_names


# ---------------------------------------------------------------------------
# Rust parsing
# ---------------------------------------------------------------------------


@_skip_no_rust
def test_parse_rust_functions(rust_module: Path):
    """Rust: parse_file extracts functions."""
    result = parse_file(rust_module)
    func_names = {s.name for s in result.symbols if s.kind == "function"}
    assert "add" in func_names
    assert "multiply" in func_names
    assert "multiply_internal" in func_names


@_skip_no_rust
def test_parse_rust_function_calls(rust_module: Path):
    """Rust: function calls are extracted."""
    result = parse_file(rust_module)
    multiply = [s for s in result.symbols if s.name == "multiply"]
    assert len(multiply) == 1
    assert "multiply_internal" in multiply[0].calls
    # add() doesn't call anything
    add = [s for s in result.symbols if s.name == "add"]
    assert len(add) == 1
    assert len(add[0].calls) == 0


@_skip_no_rust
def test_parse_rust_struct(rust_module: Path):
    """Rust: parse_file extracts struct."""
    result = parse_file(rust_module)
    structs = [s for s in result.symbols if s.kind == "class"]
    struct_names = {s.name for s in structs}
    assert "Config" in struct_names


@_skip_no_rust
def test_parse_rust_impl_methods(rust_module: Path):
    """Rust: impl methods are extracted as methods of the struct."""
    result = parse_file(rust_module)
    methods = [
        s for s in result.symbols if s.kind == "method" and s.parent_class == "Config"
    ]
    method_names = {s.name for s in methods}
    assert "new" in method_names
    assert "with_max" in method_names
    # Verify parent class linkage
    for m in methods:
        assert m.parent_class == "Config"


@_skip_no_rust
def test_parse_rust_enum(rust_module: Path):
    """Rust: enum is extracted as a class."""
    result = parse_file(rust_module)
    enums = [s for s in result.symbols if s.name == "Operation"]
    assert len(enums) == 1
    assert enums[0].kind == "class"


@_skip_no_rust
def test_parse_rust_trait(rust_module: Path):
    """Rust: trait with method signature is extracted."""
    result = parse_file(rust_module)
    trait = [s for s in result.symbols if s.name == "Calculator"]
    assert len(trait) == 1
    # trait items are symbols in our model
    trait_methods = [
        s
        for s in result.symbols
        if s.kind == "method" and s.parent_class == "Calculator"
    ]
    assert len(trait_methods) == 1
    assert trait_methods[0].name == "calculate"


@_skip_no_rust
def test_parse_rust_simple(rust_simple: Path):
    """Rust: minimal file parses correctly."""
    result = parse_file(rust_simple)
    funcs = [s for s in result.symbols if s.kind == "function"]
    assert len(funcs) == 1
    assert funcs[0].name == "main"


# ---------------------------------------------------------------------------
# build_index with multi-language
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (
        _has_grammar("javascript")
        and _has_grammar("typescript")
        and _has_grammar("rust")
    ),
    reason="Need all three tree-sitter grammars",
)
def test_build_index_multilang(multilang_project: Path):
    """build_index handles JS, TS, and Rust files in the same directory."""
    index = build_index(multilang_project)
    names = set(index.all_names())
    # JS
    assert "run" in names
    assert "App" in names
    # TS
    assert "loadConfig" in names
    # Rust
    assert "init" in names
    assert "State" in names


# ---------------------------------------------------------------------------
# Graceful fallback
# ---------------------------------------------------------------------------


def test_parse_file_unknown_extension_fallback(tmp_path: Path):
    """Unknown extensions fall back to Python parser gracefully."""
    f = tmp_path / "script.c"
    f.write_text("int main() { return 0; }")
    result = parse_file(f)
    # Should not crash — falls back to Python parser which can't parse C
    assert isinstance(result.symbols, list)


def test_parse_file_empty(tmp_path: Path):
    """Empty file returns empty result."""
    f = tmp_path / "empty.py"
    f.write_text("")
    result = parse_file(f)
    assert result.symbols == []


def test_tree_sitter_parse_unknown_lang():
    """Unknown language falls back gracefully."""
    code = b"some code"
    root, parser = _tree_sitter_parse(code, lang_name="fortran")
    # Should fall back to Python or return None
    # Either is acceptable — main thing is no crash
    assert root is None or root.type in ("program", "module", "source_file", "ERROR")


# ---------------------------------------------------------------------------
# Symbol quality checks
# ---------------------------------------------------------------------------


@_skip_no_js
def test_js_symbol_locations(js_module: Path):
    """JavaScript: symbol locations are non-zero."""
    result = parse_file(js_module)
    for s in result.symbols:
        assert s.start_line >= 1
        assert s.end_line >= s.start_line
        assert s.file == str(js_module)


@_skip_no_rust
def test_rust_symbol_locations(rust_module: Path):
    """Rust: symbol locations are non-zero."""
    result = parse_file(rust_module)
    for s in result.symbols:
        assert s.start_line >= 1
        assert s.end_line >= s.start_line
        assert s.file == str(rust_module)


@_skip_no_rust
def test_parse_rust_impl_method_calls(rust_module: Path):
    """Rust: calls are extracted from impl method bodies."""
    result = parse_file(rust_module)
    # add() body is `a + b` — no calls expected (no call_expression)
    add_sym = next((s for s in result.symbols if s.name == "add"), None)
    assert add_sym is not None, "add symbol not found in parse result"
    assert add_sym.calls == []
    # with_max() body is `self.max_value = max; self` — no call_expression
    with_max = next((s for s in result.symbols if s.name == "with_max"), None)
    assert with_max is not None, "with_max symbol not found in parse result"
    assert with_max.calls == []


@_skip_no_rust
def test_parse_rust_receiver_method_calls_link_call_graph(tmp_path: Path):
    """Rust: self/Self method calls are linked to local impl methods."""
    rust_file = tmp_path / "receiver_calls.rs"
    rust_file.write_text(
        """\
struct Client;

impl Client {
    fn run(&self) -> i32 {
        self.prepare();
        Self::make();
        helper()
    }

    fn prepare(&self) {}

    fn make() -> i32 {
        1
    }
}

fn helper() -> i32 {
    1
}
"""
    )
    result = parse_file(rust_file)
    run = next(
        (s for s in result.symbols if s.name == "run" and s.parent_class == "Client"),
        None,
    )
    assert run is not None, "run method not found in parse result"
    assert "self.prepare" in run.calls
    assert "Client.prepare" in run.calls
    assert "Self::make" in run.calls
    assert "Client.make" in run.calls

    callees, callers = build_call_graph(result.symbols)
    assert "::Client.prepare" in callees["::Client.run"]
    assert "::Client.make" in callees["::Client.run"]
    assert "::helper" in callees["::Client.run"]
    assert "::Client.run" in callers["::Client.prepare"]
    assert "::Client.run" in callers["::Client.make"]


# ---------------------------------------------------------------------------
# Rust import extraction
# ---------------------------------------------------------------------------


@pytest.fixture
def rust_imports() -> Generator[Path, None, None]:
    """A Rust file with various import patterns."""
    code = """\
// imports.rs — various import styles

use std::collections::HashMap;
use crate::utils;
use super::*;
use foo::bar as baz;
use std::io::{self, BufRead};
use serde::{Serialize, Deserialize};
use embedded_hal::i2c::{I2c, SevenBitAddress};
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rs", delete=False, prefix="test_rust_imports_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_rust
def test_parse_rust_imports_basic(rust_module: Path):
    """Rust: parse_file extracts imports from use statements."""
    result = parse_file(rust_module)
    imports = result.imports
    # rust_module has: use std::fmt; use std::io::{self, Write};
    assert len(imports) >= 2

    # Check std::fmt import: module is the parent ("std"), name is the leaf ("fmt")
    fmt_imports = [i for i in imports if i.name == "fmt" and i.module == "std"]
    assert len(fmt_imports) == 1
    assert fmt_imports[0].is_from is True

    # Check std::io imports (self and Write — rust_module has `use std::io::{self, Write}`)
    io_imports = [i for i in imports if i.module == "std::io"]
    assert len(io_imports) == 2
    io_names = {i.name for i in io_imports}
    assert "self" in io_names
    assert "Write" in io_names


@_skip_no_rust
def test_parse_rust_imports_variants(rust_imports: Path):
    """Rust: dense import patterns (use_list, as-clause, wildcard)."""
    result = parse_file(rust_imports)
    imports = result.imports

    # Simple path: use std::collections::HashMap
    # module = parent path ("std::collections"), name = leaf ("HashMap")
    hm = [i for i in imports if i.name == "HashMap"]
    assert len(hm) == 1
    assert hm[0].module == "std::collections"
    assert hm[0].is_from is True

    # Crate-relative: use crate::utils → module="crate", name="utils"
    utils = [i for i in imports if i.name == "utils"]
    assert len(utils) >= 1
    crate = [i for i in utils if i.module == "crate"]
    assert len(crate) == 1

    # As-clause: use foo::bar as baz → module="foo", name="bar", alias="baz"
    baz = [i for i in imports if i.name == "bar" and i.alias == "baz"]
    assert len(baz) == 1
    assert baz[0].module == "foo"

    # Nested use_list: use std::io::{self, BufRead} → module="std::io" (exact)
    io_path = [i for i in imports if i.module == "std::io"]
    assert len(io_path) >= 2

    # Nested use_list 2 levels: use embedded_hal::i2c::{I2c, SevenBitAddress}
    i2c_path = [i for i in imports if i.module == "embedded_hal::i2c"]
    assert len(i2c_path) >= 2

    # Single-segment module prefix: use serde::{Serialize, Deserialize}
    # module = "serde" (bare identifier, P2 fix), not "" or None
    serde_path = [i for i in imports if i.module == "serde"]
    assert len(serde_path) >= 2
    serde_names = {i.name for i in serde_path}
    assert "Serialize" in serde_names
    assert "Deserialize" in serde_names


@_skip_no_rust
def test_parse_rust_imports_glob(rust_imports: Path):
    """Rust: wildcard imports (use super::*) are extracted."""
    result = parse_file(rust_imports)
    super_wild = [i for i in result.imports if i.module == "super" and i.name == "*"]
    assert len(super_wild) == 1
    assert super_wild[0].is_from is True


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

_skip_no_go = pytest.mark.skipif(
    not _has_grammar("go"),
    reason="tree-sitter-go not installed",
)

_GO_MODULE = """\
package main

import (
\t"fmt"
\tm "math"
\t. "os"
\t_ "io"
)

import "strings"

type Calculator struct {
\tprecision int
}

type Adder interface {
\tAdd(a, b int) int
}

func NewCalc(p int) *Calculator {
\treturn &Calculator{precision: p}
}

func (c *Calculator) Add(a, b int) int {
\tfmt.Println("adding")
\treturn a + b
}

func (c Calculator) Precision() int {
\treturn c.precision
}

func helper() {
\tm.Sqrt(2.0)
\tstrings.Join(nil, "")
}
"""


@pytest.fixture
def go_module() -> Generator[Path, None, None]:
    """A temporary Go source file with functions, methods, and types."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".go", delete=False, prefix="test_go_"
    ) as f:
        f.write(_GO_MODULE)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_go
def test_language_detection_go(go_module: Path):
    """parse_file detects .go files."""
    result = parse_file(go_module)
    assert result.language == "go"


@_skip_no_go
def test_parse_go_functions(go_module: Path):
    """Go: top-level functions are extracted."""
    result = parse_file(go_module)
    names = {s.name for s in result.symbols if s.kind == "function"}
    assert "NewCalc" in names
    assert "helper" in names


@_skip_no_go
def test_parse_go_methods(go_module: Path):
    """Go: methods are extracted with correct parent class."""
    result = parse_file(go_module)
    methods = [s for s in result.symbols if s.kind == "method"]
    assert len(methods) >= 2

    add = [m for m in methods if m.name == "Add"]
    assert len(add) == 1
    assert add[0].parent_class == "Calculator"

    prec = [m for m in methods if m.name == "Precision"]
    assert len(prec) == 1
    assert prec[0].parent_class == "Calculator"


@_skip_no_go
def test_parse_go_types(go_module: Path):
    """Go: struct and interface type declarations are extracted as class."""
    result = parse_file(go_module)
    classes = {s.name for s in result.symbols if s.kind == "class"}
    assert "Calculator" in classes
    assert "Adder" in classes


@_skip_no_go
def test_parse_go_function_calls(go_module: Path):
    """Go: calls are extracted from function bodies."""
    result = parse_file(go_module)
    helper = next((s for s in result.symbols if s.name == "helper"), None)
    assert helper is not None
    assert any("Sqrt" in c for c in helper.calls)
    assert any("Join" in c for c in helper.calls)


@_skip_no_go
def test_parse_go_method_calls(go_module: Path):
    """Go: calls are extracted from method bodies."""
    result = parse_file(go_module)
    add = next(
        (s for s in result.symbols if s.name == "Add" and s.kind == "method"), None
    )
    assert add is not None
    assert any("Println" in c for c in add.calls)


@_skip_no_go
def test_parse_go_receiver_method_calls_link_call_graph(tmp_path: Path):
    """Go: receiver method calls are linked to local methods."""
    go_file = tmp_path / "receiver_calls.go"
    go_file.write_text(
        """\
package main

type Calculator struct{}

func (c *Calculator) Add(a, b int) int {
    c.log()
    return helper(a + b)
}

func (c *Calculator) log() {}

func helper(v int) int {
    return v
}
"""
    )
    result = parse_file(go_file)
    add = next(
        (
            s
            for s in result.symbols
            if s.name == "Add" and s.parent_class == "Calculator"
        ),
        None,
    )
    assert add is not None, "Add method not found in parse result"
    assert "c.log" in add.calls
    assert "Calculator.log" in add.calls

    callees, callers = build_call_graph(result.symbols)
    assert "::Calculator.log" in callees["::Calculator.Add"]
    assert "::helper" in callees["::Calculator.Add"]
    assert "::Calculator.Add" in callers["::Calculator.log"]


@_skip_no_go
def test_parse_go_receiver_aliases_skip_shadowed_locals(tmp_path: Path):
    """Go: receiver aliases don't treat same-named locals as self calls."""
    go_file = tmp_path / "receiver_shadow.go"
    go_file.write_text(
        """\
package main

type Client struct{}
type Call struct{}

func (c *Call) doX() {}

func (c *Client) Run(calls []Call) {
    c.log()
    for _, c := range calls {
        c.doX()
    }
    {
        var c Call
        c.doX()
    }
    c.finish()
}

func (c *Client) log() {}
func (c *Client) finish() {}
func (c *Client) doX() {}
"""
    )
    result = parse_file(go_file)
    run = next(
        (s for s in result.symbols if s.name == "Run" and s.parent_class == "Client"),
        None,
    )
    assert run is not None, "Run method not found in parse result"
    assert "Client.log" in run.calls
    assert "Client.finish" in run.calls
    assert "Client.doX" not in run.calls

    callees, _callers = build_call_graph(result.symbols)
    assert "::Client.log" in callees["::Client.Run"]
    assert "::Client.finish" in callees["::Client.Run"]
    assert "::Client.doX" not in callees["::Client.Run"]
    assert "::Call.doX" not in callees["::Client.Run"]


@_skip_no_go
def test_parse_go_imports_basic(go_module: Path):
    """Go: plain imports are extracted."""
    result = parse_file(go_module)
    imports = result.imports

    fmt_imp = [i for i in imports if i.name == "fmt"]
    assert len(fmt_imp) == 1
    assert fmt_imp[0].module == "fmt"
    assert fmt_imp[0].alias is None
    assert fmt_imp[0].is_from is False

    strings_imp = [i for i in imports if i.name == "strings"]
    assert len(strings_imp) == 1
    assert strings_imp[0].module == "strings"


@_skip_no_go
def test_parse_go_imports_alias(go_module: Path):
    """Go: aliased imports (import m "math") are extracted."""
    result = parse_file(go_module)
    math_imp = [i for i in result.imports if i.name == "math"]
    assert len(math_imp) == 1
    assert math_imp[0].alias == "m"
    assert math_imp[0].is_from is False


@_skip_no_go
def test_parse_go_imports_dot(go_module: Path):
    """Go: dot imports (import . "os") set is_from=True."""
    result = parse_file(go_module)
    os_imp = [i for i in result.imports if i.name == "os"]
    assert len(os_imp) == 1
    assert os_imp[0].is_from is True
    assert os_imp[0].alias is None


@_skip_no_go
def test_parse_go_imports_blank(go_module: Path):
    """Go: blank imports (import _ "io") have alias="_"."""
    result = parse_file(go_module)
    io_imp = [i for i in result.imports if i.name == "io"]
    assert len(io_imp) == 1
    assert io_imp[0].alias == "_"
    assert io_imp[0].is_from is False


# ---------------------------------------------------------------------------
# Java fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def java_module() -> Generator[Path, None, None]:
    """A Java file with a class, interface, enum, methods, and imports."""
    code = """\
package com.example;

import java.util.List;
import java.util.ArrayList;
import static java.lang.Math.PI;
import java.io.*;

public class Calculator {
    private int value;

    public Calculator(int initial) {
        this.value = initial;
    }

    public int add(int n) {
        return value + n;
    }

    public int multiply(int n) {
        return value * n;
    }

    public static Calculator fromList(List<Integer> values) {
        return new Calculator(values.get(0));
    }
}

interface Describable {
    String describe();
    int size();
}

enum Operation {
    ADD,
    MULTIPLY,
    SUBTRACT
}
"""
    with tempfile.NamedTemporaryFile(suffix=".java", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Java tests
# ---------------------------------------------------------------------------


@_skip_no_java
def test_parse_java_class(java_module: Path):
    """Java: class is extracted with correct name and kind."""
    result = parse_file(java_module)
    names = [s.name for s in result.symbols]
    assert "Calculator" in names
    calc = next(s for s in result.symbols if s.name == "Calculator")
    assert calc.kind == "class"
    assert calc.parent_class is None


@_skip_no_java
def test_parse_java_methods(java_module: Path):
    """Java: methods are extracted with parent_class set."""
    result = parse_file(java_module)
    methods = [
        s
        for s in result.symbols
        if s.kind == "method" and s.parent_class == "Calculator"
    ]
    method_names = {m.name for m in methods}
    assert "add" in method_names
    assert "multiply" in method_names
    assert "fromList" in method_names


@_skip_no_java
def test_parse_java_constructor(java_module: Path):
    """Java: constructors are extracted as methods with parent_class."""
    result = parse_file(java_module)
    ctors = [
        s
        for s in result.symbols
        if s.name == "Calculator" and s.parent_class == "Calculator"
    ]
    assert len(ctors) >= 1


@_skip_no_java
def test_parse_java_interface(java_module: Path):
    """Java: interface is extracted as a class symbol."""
    result = parse_file(java_module)
    iface = next((s for s in result.symbols if s.name == "Describable"), None)
    assert iface is not None
    assert iface.kind == "class"


@_skip_no_java
def test_parse_java_interface_methods(java_module: Path):
    """Java: interface method declarations are extracted."""
    result = parse_file(java_module)
    iface_methods = [s for s in result.symbols if s.parent_class == "Describable"]
    names = {m.name for m in iface_methods}
    assert "describe" in names
    assert "size" in names


@_skip_no_java
def test_parse_java_enum(java_module: Path):
    """Java: enum is extracted as a class symbol."""
    result = parse_file(java_module)
    enum_sym = next((s for s in result.symbols if s.name == "Operation"), None)
    assert enum_sym is not None
    assert enum_sym.kind == "class"


@_skip_no_java
def test_parse_java_imports_basic(java_module: Path):
    """Java: standard imports are extracted with correct module and name."""
    result = parse_file(java_module)
    imports = result.imports

    list_imp = next((i for i in imports if i.name == "List"), None)
    assert list_imp is not None
    assert list_imp.module == "java.util"
    assert list_imp.is_from is True

    arraylist_imp = next((i for i in imports if i.name == "ArrayList"), None)
    assert arraylist_imp is not None
    assert arraylist_imp.module == "java.util"


@_skip_no_java
def test_parse_java_imports_wildcard(java_module: Path):
    """Java: wildcard imports (java.io.*) set name='*' and correct module."""
    result = parse_file(java_module)
    wildcard = next((i for i in result.imports if i.name == "*"), None)
    assert wildcard is not None
    assert wildcard.module == "java.io"


@_skip_no_java
def test_parse_java_line_numbers(java_module: Path):
    """Java: symbol line numbers are positive and ordered."""
    result = parse_file(java_module)
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


@_skip_no_java
def test_parse_java_calls(java_module: Path):
    """Java: method_invocation nodes are extracted as calls."""
    result = parse_file(java_module)
    # fromList calls values.get(0) — a method_invocation
    from_list = next((s for s in result.symbols if s.name == "fromList"), None)
    assert from_list is not None
    assert "get" in from_list.calls, f"Expected 'get' in calls, got: {from_list.calls}"


@_skip_no_java
def test_build_index_java(java_module: Path):
    """Java: build_index picks up symbols from a .java file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / java_module.name
        shutil.copy(java_module, dest)
        index = build_index(d)
        assert "Calculator" in index.entries
        assert "add" in index.entries


@pytest.fixture
def csharp_module() -> Generator[Path, None, None]:
    """A C# file with a namespace, class, interface, enum, methods, usings."""
    code = """\
using System;
using System.Collections.Generic;
using static System.Math;
using Json = System.Text.Json;

namespace Example.App
{
    public class Calculator
    {
        private int value;

        public Calculator(int initial)
        {
            this.value = initial;
        }

        public int Add(int n)
        {
            return value + n;
        }

        public static Calculator FromList(List<int> values)
        {
            Console.WriteLine(values[0]);
            return new Calculator(values[0]);
        }
    }

    public interface IDescribable
    {
        string Describe();
        int Size();
    }

    public enum Operation
    {
        Add,
        Multiply,
        Subtract
    }
}
"""
    with tempfile.NamedTemporaryFile(suffix=".cs", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def cpp_module() -> Generator[Path, None, None]:
    """A C++ file with includes, namespace, class, methods, and free functions."""
    code = """\
#include <vector>
#include "widget.hpp"

namespace math {
class Accumulator {
public:
    Accumulator() {}
    int add(int x);
    static int helper(int x) { return x; }
};

int scale(int x) { return x * 2; }

int Accumulator::add(int x) {
    return helper(x) + scale(x);
}
}

int free_fn(int n) {
    return math::Accumulator::helper(n);
}
"""
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Ruby fixtures + tests
# ---------------------------------------------------------------------------


@pytest.fixture
def ruby_module() -> Generator[Path, None, None]:
    """A Ruby source file exercising modules, classes, methods, and requires."""
    code = """\
require "json"
require_relative "support/base"

module Geometry
  PI = 3.14159

  class Circle < Shape
    def initialize(radius)
      @radius = radius
    end

    def area
      compute(PI * @radius * @radius)
    end

    def self.unit
      new(1)
    end
  end

  def describe
    puts "geometry"
  end
end

def main
  Geometry::Circle.unit.area
end
"""
    with tempfile.NamedTemporaryFile(suffix=".rb", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# C# tests
# ---------------------------------------------------------------------------


@_skip_no_csharp
def test_language_detection_csharp(csharp_module: Path):
    """C#: .cs files are detected as the csharp language."""
    result = parse_file(csharp_module)
    assert result.language == "csharp"


@_skip_no_csharp
def test_parse_csharp_class(csharp_module: Path):
    """C#: class nested in a namespace is extracted with correct kind."""
    result = parse_file(csharp_module)
    cls = next((s for s in result.symbols if s.name == "Calculator"), None)
    assert cls is not None
    assert cls.kind == "class"


@_skip_no_csharp
def test_parse_csharp_methods(csharp_module: Path):
    """C#: methods carry their parent class."""
    result = parse_file(csharp_module)
    add = next((s for s in result.symbols if s.name == "Add"), None)
    assert add is not None
    assert add.kind == "method"
    assert add.parent_class == "Calculator"


@_skip_no_csharp
def test_parse_csharp_constructor(csharp_module: Path):
    """C#: constructor_declaration is extracted as a method."""
    result = parse_file(csharp_module)
    ctors = [s for s in result.symbols if s.name == "Calculator" and s.kind == "method"]
    assert len(ctors) == 1
    assert ctors[0].parent_class == "Calculator"


@_skip_no_csharp
def test_parse_csharp_interface(csharp_module: Path):
    """C#: interface is extracted as a class symbol with its methods."""
    result = parse_file(csharp_module)
    iface = next((s for s in result.symbols if s.name == "IDescribable"), None)
    assert iface is not None
    assert iface.kind == "class"
    describe = next((s for s in result.symbols if s.name == "Describe"), None)
    assert describe is not None
    assert describe.parent_class == "IDescribable"


@_skip_no_csharp
def test_parse_csharp_enum(csharp_module: Path):
    """C#: enum is extracted as a class symbol."""
    result = parse_file(csharp_module)
    enum_sym = next((s for s in result.symbols if s.name == "Operation"), None)
    assert enum_sym is not None
    assert enum_sym.kind == "class"


@_skip_no_csharp
def test_parse_csharp_imports_basic(csharp_module: Path):
    """C#: using directives resolve to module + last-segment name."""
    result = parse_file(csharp_module)
    generic = next((i for i in result.imports if i.name == "Generic"), None)
    assert generic is not None
    assert generic.module == "System.Collections"

    system = next((i for i in result.imports if i.name == "System"), None)
    assert system is not None
    assert system.module == ""


@_skip_no_csharp
def test_parse_csharp_imports_alias(csharp_module: Path):
    """C#: aliased using (using Json = System.Text.Json) records the alias."""
    result = parse_file(csharp_module)
    aliased = next((i for i in result.imports if i.alias == "Json"), None)
    assert aliased is not None
    assert aliased.name == "Json"
    assert aliased.module == "System.Text"


@_skip_no_csharp
def test_parse_csharp_calls(csharp_module: Path):
    """C#: invocation_expression nodes are extracted as calls."""
    result = parse_file(csharp_module)
    from_list = next((s for s in result.symbols if s.name == "FromList"), None)
    assert from_list is not None
    assert any(
        "WriteLine" in c for c in from_list.calls
    ), f"Expected a WriteLine call, got: {from_list.calls}"


@_skip_no_csharp
def test_parse_csharp_line_numbers(csharp_module: Path):
    """C#: symbol line numbers are positive and ordered."""
    result = parse_file(csharp_module)
    assert result.symbols
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


@_skip_no_csharp
def test_build_index_csharp(csharp_module: Path):
    """C#: build_index picks up symbols from a .cs file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / csharp_module.name
        shutil.copy(csharp_module, dest)
        index = build_index(d)
        assert "Calculator" in index.entries
        assert "Add" in index.entries


@_skip_no_ruby
def test_parse_ruby_module(ruby_module: Path):
    """Ruby: module is extracted as a class-kind namespace symbol."""
    result = parse_file(ruby_module)
    geometry = next((s for s in result.symbols if s.name == "Geometry"), None)
    assert geometry is not None
    assert geometry.kind == "class"
    assert geometry.parent_class is None


@_skip_no_ruby
def test_parse_ruby_class(ruby_module: Path):
    """Ruby: a class nested in a module records the module as parent."""
    result = parse_file(ruby_module)
    circle = next((s for s in result.symbols if s.name == "Circle"), None)
    assert circle is not None
    assert circle.kind == "class"
    assert circle.parent_class == "Geometry"


@_skip_no_ruby
def test_parse_ruby_methods(ruby_module: Path):
    """Ruby: instance methods are extracted with their class as parent."""
    result = parse_file(ruby_module)
    area = next((s for s in result.symbols if s.name == "area"), None)
    assert area is not None
    assert area.kind == "method"
    assert area.parent_class == "Circle"


@_skip_no_ruby
def test_parse_ruby_singleton_method(ruby_module: Path):
    """Ruby: def self.x singleton methods are extracted."""
    result = parse_file(ruby_module)
    unit = next((s for s in result.symbols if s.name == "unit"), None)
    assert unit is not None
    assert unit.kind == "method"
    assert unit.parent_class == "Circle"


@_skip_no_ruby
def test_parse_ruby_module_level_method(ruby_module: Path):
    """Ruby: a def directly inside a module is parented to the module."""
    result = parse_file(ruby_module)
    describe = next((s for s in result.symbols if s.name == "describe"), None)
    assert describe is not None
    assert describe.parent_class == "Geometry"


@_skip_no_ruby
def test_parse_ruby_top_level_function(ruby_module: Path):
    """Ruby: a top-level def has no parent and is a function."""
    result = parse_file(ruby_module)
    main = next((s for s in result.symbols if s.name == "main"), None)
    assert main is not None
    assert main.kind == "function"
    assert main.parent_class is None


@_skip_no_ruby
def test_parse_ruby_calls(ruby_module: Path):
    """Ruby: call nodes (method field) are extracted into calls."""
    result = parse_file(ruby_module)
    area = next((s for s in result.symbols if s.name == "area"), None)
    assert area is not None
    assert "compute" in area.calls, f"Expected 'compute' in calls, got: {area.calls}"


@_skip_no_ruby
def test_parse_ruby_requires(ruby_module: Path):
    """Ruby: require and require_relative are extracted as imports."""
    result = parse_file(ruby_module)
    modules = {imp.module for imp in result.imports}
    assert "json" in modules
    assert "support/base" in modules


@_skip_no_ruby
def test_parse_ruby_line_numbers(ruby_module: Path):
    """Ruby: symbols carry sensible 1-based line numbers."""
    result = parse_file(ruby_module)
    circle = next((s for s in result.symbols if s.name == "Circle"), None)
    assert circle is not None
    assert circle.start_line >= 1
    assert circle.end_line >= circle.start_line


@_skip_no_ruby
def test_build_index_ruby(ruby_module: Path):
    """Ruby: build_index picks up symbols from a .rb file."""

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / ruby_module.name
        shutil.copy(ruby_module, dest)
        index = build_index(d)
        assert "Circle" in index.entries
        assert "area" in index.entries


_skip_no_c = pytest.mark.skipif(
    not _has_grammar("c"),
    reason="tree-sitter-c not installed",
)


@pytest.fixture
def c_module() -> Generator[Path, None, None]:
    """A C file with includes, a typedef'd struct, a named struct, and functions."""
    code = """\
#include <stdio.h>
#include <sys/types.h>
#include "utils.h"

typedef struct {
    int x;
    int y;
} Point;

struct Config {
    int width;
    int height;
};

static int add(int a, int b) {
    return a + b;
}

int multiply(int a, int b) {
    int result = add(a, b);
    printf("%d", result);
    return result;
}

int *alloc_point(void) {
    return NULL;
}

int main(void) {
    int r = multiply(3, 4);
    return 0;
}
"""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# C tests
# ---------------------------------------------------------------------------


@_skip_no_c
def test_language_detection_c(c_module: Path):
    """C: .c files are detected as the c language."""
    result = parse_file(c_module)
    assert result.language == "c"


@_skip_no_c
def test_parse_c_functions(c_module: Path):
    """C: function definitions are extracted with kind='function'."""
    result = parse_file(c_module)
    names = [s.name for s in result.symbols]
    assert "add" in names
    assert "multiply" in names
    assert "main" in names
    for s in result.symbols:
        if s.name in ("add", "multiply", "main"):
            assert s.kind == "function"


@_skip_no_c
def test_parse_c_pointer_return(c_module: Path):
    """C: function with pointer return type (int *foo) is still extracted."""
    result = parse_file(c_module)
    assert any(s.name == "alloc_point" for s in result.symbols)


@_skip_no_c
def test_parse_c_typedef_struct(c_module: Path):
    """C: typedef struct { ... } Name is extracted as kind='class'."""
    result = parse_file(c_module)
    sym = next((s for s in result.symbols if s.name == "Point"), None)
    assert sym is not None
    assert sym.kind == "class"


@_skip_no_c
def test_parse_c_named_struct(c_module: Path):
    """C: named struct declarations are extracted as kind='class'."""
    result = parse_file(c_module)
    sym = next((s for s in result.symbols if s.name == "Config"), None)
    assert sym is not None
    assert sym.kind == "class"


@_skip_no_c
def test_parse_c_calls(c_module: Path):
    """C: function calls inside a body are extracted."""
    result = parse_file(c_module)
    multiply_sym = next((s for s in result.symbols if s.name == "multiply"), None)
    assert multiply_sym is not None
    assert any(
        "add" in c for c in multiply_sym.calls
    ), f"Expected 'add' in calls, got: {multiply_sym.calls}"
    assert any(
        "printf" in c for c in multiply_sym.calls
    ), f"Expected 'printf' in calls, got: {multiply_sym.calls}"


@_skip_no_c
def test_parse_c_imports_stdlib(c_module: Path):
    """C: angle-bracket includes are extracted with correct name."""
    result = parse_file(c_module)
    stdio = next((i for i in result.imports if i.name == "stdio.h"), None)
    assert stdio is not None
    assert stdio.module == ""


@_skip_no_c
def test_parse_c_imports_nested(c_module: Path):
    """C: nested include paths (sys/types.h) split into module and name."""
    result = parse_file(c_module)
    types = next((i for i in result.imports if i.name == "types.h"), None)
    assert types is not None
    assert types.module == "sys"


@_skip_no_c
def test_parse_c_imports_local(c_module: Path):
    """C: quoted local includes are extracted."""
    result = parse_file(c_module)
    utils = next((i for i in result.imports if i.name == "utils.h"), None)
    assert utils is not None


@_skip_no_c
def test_parse_c_line_numbers(c_module: Path):
    """C: symbol line numbers are positive and ordered."""
    result = parse_file(c_module)
    assert result.symbols
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------


@_skip_no_cpp
def test_language_detection_cpp(cpp_module: Path):
    """C++: .cpp files are detected as the cpp language."""
    result = parse_file(cpp_module)
    assert result.language == "cpp"


@_skip_no_cpp
def test_language_detection_cpp_header(tmp_path: Path):
    """C++: .h headers are treated as C++ source files."""
    header = tmp_path / "widget.h"
    header.write_text("class Widget {};\n")

    result = parse_file(header)

    assert result.language == "cpp"
    assert any(sym.name == "Widget" for sym in result.symbols)


@_skip_no_cpp
def test_parse_cpp_class_and_functions(cpp_module: Path):
    """C++: class and top-level functions are extracted."""
    result = parse_file(cpp_module)

    classes = {s.name for s in result.symbols if s.kind == "class"}
    assert "Accumulator" in classes

    functions = {s.name for s in result.symbols if s.kind == "function"}
    assert "scale" in functions
    assert "free_fn" in functions


@_skip_no_cpp
def test_parse_cpp_methods(cpp_module: Path):
    """C++: inline and out-of-class methods carry their parent class."""
    result = parse_file(cpp_module)
    methods = [s for s in result.symbols if s.kind == "method"]
    method_names = {s.name for s in methods}

    assert "Accumulator" in method_names
    assert "add" in method_names
    assert "helper" in method_names

    add = next((s for s in methods if s.name == "add"), None)
    assert add is not None
    assert add.parent_class == "Accumulator"


@_skip_no_cpp
def test_parse_cpp_calls(cpp_module: Path):
    """C++: call_expression nodes are extracted from methods and functions."""
    result = parse_file(cpp_module)

    add = next((s for s in result.symbols if s.name == "add"), None)
    assert add is not None
    assert "helper" in add.calls
    assert "scale" in add.calls

    free_fn = next((s for s in result.symbols if s.name == "free_fn"), None)
    assert free_fn is not None
    assert "math::Accumulator::helper" in free_fn.calls


@_skip_no_cpp
def test_parse_cpp_imports(cpp_module: Path):
    """C++: #include directives resolve to import records."""
    result = parse_file(cpp_module)

    vector = next((i for i in result.imports if i.name == "vector"), None)
    assert vector is not None
    assert vector.module == ""

    widget = next((i for i in result.imports if i.name == "widget.hpp"), None)
    assert widget is not None
    assert widget.module == ""


@_skip_no_cpp
def test_parse_cpp_line_numbers(cpp_module: Path):
    """C++: symbol line numbers are positive and ordered."""
    result = parse_file(cpp_module)
    assert result.symbols
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


@pytest.fixture
def cpp_conditional_module() -> Generator[Path, None, None]:
    """A C++ file with preprocessor-conditional includes and a conditionally-compiled class."""
    code = """\
#include <vector>
#ifdef _WIN32
#include <windows.h>
#endif
#ifndef NO_EXTRA
#include <extra.hpp>
#endif
#if defined(FEATURE_X)
#include <feature.hpp>
class FeatureX {};
#endif
#ifdef USE_WIDGET
class Widget {};
#endif
class Base {};
"""
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_cpp
def test_parse_cpp_preprocessor_conditional(cpp_conditional_module: Path):
    """C++: #include inside #ifdef/#ifndef/#if is extracted via recursive walk."""
    result = parse_file(cpp_conditional_module)

    import_names = {i.name for i in result.imports}
    assert "vector" in import_names, "top-level include missing"
    assert "windows.h" in import_names, "#ifdef-guarded include missing"
    assert "extra.hpp" in import_names, "#ifndef-guarded include missing"
    assert "feature.hpp" in import_names, "#if defined(...)-guarded include missing"


@_skip_no_c
def test_build_index_c(c_module: Path):
    """C: build_index picks up functions from a .c file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / c_module.name
        shutil.copy(c_module, dest)
        index = build_index(d)
        assert "add" in index.entries
        assert "multiply" in index.entries


@pytest.fixture
def c_conditional_module() -> Generator[Path, None, None]:
    """A C file with preprocessor-conditional includes and a conditionally-compiled function."""
    code = """\
#include <stdio.h>
#ifdef _WIN32
#include <windows.h>
#endif
#ifndef NO_EXTRA
#include <extra.h>
#endif
#if defined(FEATURE_X)
#include <feature.h>
int feature_x_init(void) { return 0; }
#endif
#ifdef USE_BAR
int bar(void) { return 2; }
#endif
int main(void) { return 0; }
"""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_c
def test_parse_c_preprocessor_conditional(c_conditional_module: Path):
    """C: #include inside #ifdef/#ifndef/#if is extracted via recursive walk."""
    result = parse_file(c_conditional_module)

    import_names = {i.name for i in result.imports}
    assert "stdio.h" in import_names, "top-level include missing"
    assert "windows.h" in import_names, "#ifdef-guarded include missing"
    assert "extra.h" in import_names, "#ifndef-guarded include missing"
    assert "feature.h" in import_names, "#if defined(...)-guarded include missing"

    symbol_names = {s.name for s in result.symbols}
    assert "feature_x_init" in symbol_names, "function inside #if defined(...) missing"
    assert "bar" in symbol_names, "function inside #ifdef missing"
    assert "main" in symbol_names


@_skip_no_c
def test_parse_c_parenthesized_declarator(c_conditional_module: Path):
    """C: int (*(foo))(void) — parenthesized_declarator wraps a pointer_declarator."""
    # The default c_module fixture doesn't have this pattern, but our
    # _c_find_function_name handles it.  Test with a separate fixture.
    code = "int (*(inner_func))(void) { return NULL; }\n"
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    try:
        result = parse_file(path)
        assert any(
            s.name == "inner_func" for s in result.symbols
        ), f"Expected inner_func in symbols, got: {[s.name for s in result.symbols]}"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PHP extraction tests
# ---------------------------------------------------------------------------


@pytest.fixture
def php_module() -> Generator[Path, None, None]:
    """A PHP file with namespaces, use imports, classes, methods, interface, trait, and functions."""
    code = """<?php

namespace App\\Service;

use App\\Entity\\User;
use App\\Util\\Helper as H;
use function array_map;
use const PHP_EOL;

class Calculator {
    private int $value;

    public function __construct(int $initialValue = 0) {
        $this->value = $initialValue;
    }

    public function add(int $n): self {
        $this->value += $n;
        return $this;
    }

    public function getValue(): int {
        return $this->value;
    }

    public static function create(int $val): self {
        return new self($val);
    }
}

interface Describable {
    public function describe(): string;
}

trait Loggable {
    public function log(string $msg): void {
        echo $msg;
    }
}

function helper(string $name): string {
    return "Hello " . $name;
}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".php", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_php
def test_language_detection_php(php_module: Path):
    """PHP: language is detected from .php extension."""
    from gptme_codegraph.core import _detect_language

    assert _detect_language(php_module) == "php"


@_skip_no_php
def test_parse_php_class(php_module: Path):
    """PHP: class is extracted with correct name and kind."""
    result = parse_file(php_module)
    classes = [s for s in result.symbols if s.kind == "class"]
    class_names = {s.name for s in classes}
    assert "Calculator" in class_names, f"Expected Calculator class, got {class_names}"
    assert (
        "Describable" in class_names
    ), f"Expected Describable interface, got {class_names}"
    assert "Loggable" in class_names, f"Expected Loggable trait, got {class_names}"
    calc = next(s for s in classes if s.name == "Calculator")
    assert calc.parent_class is None


@_skip_no_php
def test_parse_php_methods(php_module: Path):
    """PHP: methods are extracted with parent_class set."""
    result = parse_file(php_module)
    methods = [s for s in result.symbols if s.kind == "method"]
    method_names = {(s.name, s.parent_class) for s in methods}
    assert ("__construct", "Calculator") in method_names
    assert ("add", "Calculator") in method_names
    assert ("getValue", "Calculator") in method_names
    assert ("create", "Calculator") in method_names
    assert ("describe", "Describable") in method_names
    assert ("log", "Loggable") in method_names


@_skip_no_php
def test_parse_php_function(php_module: Path):
    """PHP: top-level function is extracted as function kind."""
    result = parse_file(php_module)
    funcs = [s for s in result.symbols if s.kind == "function"]
    func_names = {s.name for s in funcs}
    assert "helper" in func_names, f"Expected helper function, got {func_names}"


@_skip_no_php
def test_parse_php_imports(php_module: Path):
    """PHP: use declarations are extracted as imports."""
    result = parse_file(php_module)
    import_names = {(imp.name, imp.module, imp.alias) for imp in result.imports}
    assert (
        "User",
        "App\\Entity",
        None,
    ) in import_names, f"Expected use App\\Entity\\User, got {import_names}"
    assert (
        "Helper",
        "App\\Util",
        "H",
    ) in import_names, f"Expected use App\\Util\\Helper as H, got {import_names}"
    assert "User" in {imp.name for imp in result.imports}, "User import missing"
    assert "Helper" in {imp.name for imp in result.imports}, "Helper import missing"


@_skip_no_php
def test_parse_php_function_imports(php_module: Path):
    """PHP: use function declarations are extracted."""
    result = parse_file(php_module)
    func_imports = [imp for imp in result.imports if imp.name == "array_map"]
    assert (
        len(func_imports) >= 1
    ), f"Expected use function array_map, got {[(imp.name, imp.module) for imp in result.imports]}"


@_skip_no_php
def test_parse_php_const_imports(php_module: Path):
    """PHP: use const declarations are extracted."""
    result = parse_file(php_module)
    const_imports = [imp for imp in result.imports if imp.name == "PHP_EOL"]
    assert (
        len(const_imports) >= 1
    ), f"Expected use const PHP_EOL, got {[(imp.name, imp.module) for imp in result.imports]}"


@_skip_no_php
def test_parse_php_namespaced_function_const_imports(tmp_path: Path):
    """PHP: namespaced function/const imports split module and symbol name."""
    code = """<?php
use function App\\Support\\array_map;
use const App\\Support\\PHP_EOL;
"""
    php_file = tmp_path / "function_const_imports.php"
    php_file.write_text(code)
    result = parse_file(php_file)
    import_names = {(imp.name, imp.module, imp.alias) for imp in result.imports}
    assert (
        "array_map",
        "App\\Support",
        None,
    ) in import_names, f"Expected namespaced function import, got {import_names}"
    assert (
        "PHP_EOL",
        "App\\Support",
        None,
    ) in import_names, f"Expected namespaced const import, got {import_names}"


@_skip_no_php
def test_parse_php_line_numbers(php_module: Path):
    """PHP: symbol line numbers are correct."""
    result = parse_file(php_module)
    calc = next(
        s for s in result.symbols if s.name == "Calculator" and s.kind == "class"
    )
    assert (
        calc.start_line == 10
    ), f"Expected Calculator at line 10, got {calc.start_line}"
    add = next(
        s for s in result.symbols if s.name == "add" and s.parent_class == "Calculator"
    )
    assert add.start_line == 17, f"Expected add at line 17, got {add.start_line}"
    helper = next(s for s in result.symbols if s.name == "helper")
    assert (
        helper.start_line == 41
    ), f"Expected helper at line 42, got {helper.start_line}"


@_skip_no_php
def test_build_index_php(php_module: Path):
    """PHP: build_index works for a PHP file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        shutil.copy(php_module, d)
        index = build_index(d)
        assert index.lookup("Calculator"), "Calculator not found in index"
        assert index.lookup("helper"), "helper not found in index"


@_skip_no_php
def test_parse_php_method_calls(tmp_path: Path):
    """PHP: -> method calls are extracted from method bodies."""
    code = """<?php
class Worker {
    public function run(): void {
        $calc = new Calculator();
        $calc->add(5);
        $this->getValue();
        Calculator::create(10);
    }
}
"""
    php_file = tmp_path / "worker.php"
    php_file.write_text(code)
    result = parse_file(php_file)
    run_method = next(s for s in result.symbols if s.name == "run")
    assert "add" in run_method.calls, f"Expected 'add' in calls, got {run_method.calls}"
    assert (
        "getValue" in run_method.calls
    ), f"Expected 'getValue' in calls, got {run_method.calls}"
    assert (
        "create" in run_method.calls
    ), f"Expected 'create' in calls, got {run_method.calls}"


@_skip_no_php
def test_parse_php_group_use(tmp_path: Path):
    """PHP: group use declarations (use Ns\\{A, B}) are extracted."""
    code = """<?php
use App\\Entity\\{User, Group};
"""
    php_file = tmp_path / "group_use.php"
    php_file.write_text(code)
    result = parse_file(php_file)
    import_names = {imp.name for imp in result.imports}
    assert "User" in import_names, f"Expected 'User' in imports, got {import_names}"
    assert "Group" in import_names, f"Expected 'Group' in imports, got {import_names}"


@_skip_no_php
def test_parse_php_bracket_namespace_symbols_and_imports(tmp_path: Path):
    """PHP: bracket-style namespaces expose nested symbols and imports."""
    code = """<?php
namespace Foo {
    use App\\Entity\\User;

    class Bar {}

    function baz() {}
}
"""
    php_file = tmp_path / "bracket_namespace.php"
    php_file.write_text(code)
    result = parse_file(php_file)
    symbol_names = {(symbol.name, symbol.kind) for symbol in result.symbols}
    import_names = {(imp.name, imp.module) for imp in result.imports}
    assert ("Bar", "class") in symbol_names, f"Expected Bar class, got {symbol_names}"
    assert (
        "baz",
        "function",
    ) in symbol_names, f"Expected baz function, got {symbol_names}"
    assert (
        "User",
        "App\\Entity",
    ) in import_names, f"Expected User import, got {import_names}"


@_skip_no_php
def test_parse_php_function_call_expressions(tmp_path: Path):
    """PHP: standalone function calls (function_call_expression) are extracted from call graph."""
    code = """<?php
function processItems(array $items): void {
    $result = array_map(fn($x) => $x * 2, $items);
    sort($result);
    doSomethingElse();
}
"""
    php_file = tmp_path / "func_calls.php"
    php_file.write_text(code)
    result = parse_file(php_file)
    proc = next(s for s in result.symbols if s.name == "processItems")
    assert "array_map" in proc.calls, f"Expected 'array_map' in calls, got {proc.calls}"
    assert "sort" in proc.calls, f"Expected 'sort' in calls, got {proc.calls}"
    assert (
        "doSomethingElse" in proc.calls
    ), f"Expected 'doSomethingElse' in calls, got {proc.calls}"


# ---------------------------------------------------------------------------
# Kotlin extraction tests
# ---------------------------------------------------------------------------


@pytest.fixture
def kotlin_module() -> Generator[Path, None, None]:
    """A Kotlin file with imports, classes, methods, object, and top-level functions."""
    code = """\
package app.service

import kotlin.math.max
import app.model.User as DomainUser
import app.util.*

class Calculator(private var value: Int = 0) {
    fun add(n: Int): Calculator {
        value += n
        log("added")
        return this
    }

    fun current(): Int = value

    fun clamped(n: Int): Int = max(n, 0)

    companion object {
        fun create(initial: Int): Calculator {
            return Calculator(initial)
        }
    }
}

interface Describable {
    fun describe(): String
}

object Logger {
    fun log(msg: String) {
        println(msg)
    }
}

fun helper(name: String): String {
    return name.trim().uppercase()
}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kt", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_kotlin
def test_language_detection_kotlin(kotlin_module: Path):
    """Kotlin: .kt files are detected as Kotlin."""
    result = parse_file(kotlin_module)
    assert result.language == "kotlin"


@_skip_no_kotlin
def test_language_detection_kotlin_script(tmp_path: Path):
    """Kotlin: .kts scripts are detected as Kotlin."""
    script = tmp_path / "build.kts"
    script.write_text("fun configure() {}\n")
    result = parse_file(script)
    assert result.language == "kotlin"
    assert any(sym.name == "configure" for sym in result.symbols)


@_skip_no_kotlin
def test_parse_kotlin_classes_and_objects(kotlin_module: Path):
    """Kotlin: classes, interfaces, and objects are extracted as class symbols."""
    result = parse_file(kotlin_module)
    classes = {s.name for s in result.symbols if s.kind == "class"}
    assert "Calculator" in classes
    assert "Describable" in classes
    assert "Logger" in classes


@_skip_no_kotlin
def test_parse_kotlin_methods(kotlin_module: Path):
    """Kotlin: member, companion object, and interface functions are methods."""
    result = parse_file(kotlin_module)
    methods = {(s.name, s.parent_class) for s in result.symbols if s.kind == "method"}
    assert ("add", "Calculator") in methods
    assert ("current", "Calculator") in methods
    assert ("create", "Calculator") in methods
    assert ("describe", "Describable") in methods
    assert ("log", "Logger") in methods


@_skip_no_kotlin
def test_parse_kotlin_top_level_function(kotlin_module: Path):
    """Kotlin: top-level functions are extracted as functions."""
    result = parse_file(kotlin_module)
    functions = {s.name for s in result.symbols if s.kind == "function"}
    assert "helper" in functions


@_skip_no_kotlin
def test_parse_kotlin_imports(kotlin_module: Path):
    """Kotlin: imports include module/name split, aliases, and wildcards."""
    result = parse_file(kotlin_module)
    imports = {(imp.module, imp.name, imp.alias) for imp in result.imports}
    assert ("kotlin.math", "max", None) in imports
    assert ("app.model", "User", "DomainUser") in imports
    assert ("app.util", "*", None) in imports


@_skip_no_kotlin
def test_parse_kotlin_calls(kotlin_module: Path):
    """Kotlin: call expressions are extracted from function bodies."""
    result = parse_file(kotlin_module)
    add = next(s for s in result.symbols if s.name == "add")
    assert "log" in add.calls
    create = next(s for s in result.symbols if s.name == "create")
    assert "Calculator" in create.calls
    helper = next(s for s in result.symbols if s.name == "helper")
    assert any("trim" in call for call in helper.calls)
    assert any("uppercase" in call for call in helper.calls)


@_skip_no_kotlin
def test_parse_kotlin_expression_body_calls(kotlin_module: Path):
    """Kotlin: calls in expression-body functions (fun f() = expr) are captured."""
    result = parse_file(kotlin_module)
    clamped = next(s for s in result.symbols if s.name == "clamped")
    assert (
        "max" in clamped.calls
    ), f"expression-body call 'max' not captured; got calls={clamped.calls!r}"


@_skip_no_kotlin
def test_parse_kotlin_line_numbers(kotlin_module: Path):
    """Kotlin: symbol line numbers are positive and ordered."""
    result = parse_file(kotlin_module)
    assert result.symbols
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


@_skip_no_kotlin
def test_build_index_kotlin(kotlin_module: Path):
    """Kotlin: build_index picks up symbols from .kt files."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        shutil.copy(kotlin_module, d)
        index = build_index(d)
        assert index.lookup("Calculator"), "Calculator not found in index"
        assert index.lookup("helper"), "helper not found in index"


# ---------------------------------------------------------------------------
# Swift extraction tests
# ---------------------------------------------------------------------------


@pytest.fixture
def swift_module() -> Generator[Path, None, None]:
    """A Swift file with imports, protocols, types, methods, and functions."""
    code = """\
import Foundation
import class UIKit.UIViewController
import struct SwiftUI.View

protocol Describable {
    func describe() -> String
}

class Calculator: Describable {
    private var value: Int

    init(value: Int = 0) {
        self.value = value
    }

    func add(_ n: Int) -> Calculator {
        value += n
        log("added")
        return self
    }

    func current() -> Int { value }

    static func create(_ initial: Int) -> Calculator {
        return Calculator(value: initial)
    }
}

struct Logger {
    func log(_ message: String) {
        print(message)
    }
}

enum Mode {
    case fast
}

func helper(_ name: String) -> String {
    return name.trimmingCharacters(in: .whitespaces).uppercased()
}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".swift", delete=False) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@_skip_no_swift
def test_language_detection_swift(swift_module: Path):
    """Swift: .swift files are detected as Swift."""
    result = parse_file(swift_module)
    assert result.language == "swift"


@_skip_no_swift
def test_parse_swift_types(swift_module: Path):
    """Swift: classes, structs, enums, and protocols are class-kind symbols."""
    result = parse_file(swift_module)
    classes = {s.name for s in result.symbols if s.kind == "class"}
    assert "Calculator" in classes
    assert "Logger" in classes
    assert "Mode" in classes
    assert "Describable" in classes


@_skip_no_swift
def test_parse_swift_methods(swift_module: Path):
    """Swift: methods, initializers, static methods, and protocol functions parse."""
    result = parse_file(swift_module)
    methods = {(s.name, s.parent_class) for s in result.symbols if s.kind == "method"}
    assert ("init", "Calculator") in methods
    assert ("add", "Calculator") in methods
    assert ("current", "Calculator") in methods
    assert ("create", "Calculator") in methods
    assert ("describe", "Describable") in methods
    assert ("log", "Logger") in methods


@_skip_no_swift
def test_parse_swift_top_level_function(swift_module: Path):
    """Swift: top-level functions are extracted as functions."""
    result = parse_file(swift_module)
    functions = {s.name for s in result.symbols if s.kind == "function"}
    assert "helper" in functions


@_skip_no_swift
def test_parse_swift_imports(swift_module: Path):
    """Swift: import declarations split module and imported name."""
    result = parse_file(swift_module)
    imports = {(imp.module, imp.name, imp.alias) for imp in result.imports}
    assert ("", "Foundation", None) in imports
    assert ("UIKit", "UIViewController", None) in imports
    assert ("SwiftUI", "View", None) in imports


@_skip_no_swift
def test_parse_swift_calls(swift_module: Path):
    """Swift: simple and chained call expressions are extracted."""
    result = parse_file(swift_module)
    add = next(s for s in result.symbols if s.name == "add")
    assert "log" in add.calls
    create = next(s for s in result.symbols if s.name == "create")
    assert "Calculator" in create.calls
    log = next(s for s in result.symbols if s.name == "log")
    assert "print" in log.calls
    helper = next(s for s in result.symbols if s.name == "helper")
    assert "trimmingCharacters" in helper.calls
    assert "uppercased" in helper.calls


@_skip_no_swift
def test_parse_swift_line_numbers(swift_module: Path):
    """Swift: symbol line numbers are positive and ordered."""
    result = parse_file(swift_module)
    assert result.symbols
    for sym in result.symbols:
        assert sym.start_line >= 1
        assert sym.end_line >= sym.start_line


@_skip_no_swift
def test_build_index_swift(swift_module: Path):
    """Swift: build_index picks up symbols from a .swift file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        shutil.copy(swift_module, d)
        index = build_index(d)
        assert index.lookup("Calculator"), "Calculator not found in index"
        assert index.lookup("helper"), "helper not found in index"


@_skip_no_swift
def test_parse_swift_extension_and_actor(tmp_path: Path):
    """Swift: extensions and actors provide a parent container for methods."""
    swift_file = tmp_path / "extensions.swift"
    swift_file.write_text(
        """\
extension Calculator {
    func reset() {
        clear()
    }
}

actor Store {
    func save() {
        persist()
    }
}
"""
    )
    result = parse_file(swift_file)
    methods = {(s.name, s.parent_class) for s in result.symbols if s.kind == "method"}
    assert ("reset", "Calculator") in methods
    assert ("save", "Store") in methods
    reset = next(s for s in result.symbols if s.name == "reset")
    assert "clear" in reset.calls


# ---------------------------------------------------------------------------
@_skip_no_cpp
def test_build_index_cpp(cpp_module: Path):
    """C++: build_index picks up symbols from a .cpp file."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / cpp_module.name
        shutil.copy(cpp_module, dest)
        index = build_index(d)
        assert "Accumulator" in index.entries
        assert "add" in index.entries


@_skip_no_go
def test_parse_go_receiver_calls_no_collision_across_types(tmp_path: Path):
    """Go: receiver aliases are qualified so same-named methods across types don't collide."""
    go_file = tmp_path / "multitype.go"
    go_file.write_text(
        """\
package main

type Log struct{}
func (l *Log) format() string { return "" }

type Report struct{}
func (r *Report) format() string { return "" }

func (r *Report) Generate() string {
    r.format()
    return ""
}
"""
    )
    result = parse_file(go_file)

    report_gen = next(
        s for s in result.symbols if s.name == "Generate" and s.parent_class == "Report"
    )
    # The alias should be "Report.format", not bare "format"
    assert "Report.format" in report_gen.calls  # qualified alias
    assert "format" not in report_gen.calls  # bare name should not appear

    callees, callers = build_call_graph(result.symbols)
    # Report.Generate should call Report.format, not Log.format
    assert "::Report.format" in callees["::Report.Generate"]
    assert "::Log.format" not in callees["::Report.Generate"]


@_skip_no_rust
def test_parse_rust_receiver_calls_no_collision_across_types(tmp_path: Path):
    """Rust: receiver aliases are qualified so same-named methods across types don't collide."""
    rs_file = tmp_path / "multitype.rs"
    rs_file.write_text(
        """\
struct Counter {}
impl Counter {
    fn reset(&self) {}
    fn tick(&self) {
        self.reset();
    }
}

struct Timer {}
impl Timer {
    fn reset(&self) {}
}
"""
    )
    result = parse_file(rs_file)

    tick = next(
        s for s in result.symbols if s.name == "tick" and s.parent_class == "Counter"
    )
    # The alias should be "Counter.reset", not bare "reset"
    assert "Counter.reset" in tick.calls  # qualified alias
    assert "reset" not in tick.calls  # bare name should not appear

    callees, callers = build_call_graph(result.symbols)
    # Counter.tick should call Counter.reset, not Timer.reset
    assert "::Counter.reset" in callees["::Counter.tick"]
    assert "::Timer.reset" not in callees["::Counter.tick"]
