"""Tests for multi-language support in gptme-codegraph (JS/TS, Rust, Go, Java).

Verifies that parse_file, extract_symbols, and build_index work correctly
for JavaScript, TypeScript, Rust, Go, and Java source files.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from gptme_codegraph.core import (
    _tree_sitter_parse,
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
        "pub fn init() -> bool { true }\n" "pub struct State { pub ready: bool }\n"
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
    assert "multiply_internal" in multiply[0].calls or "self" in multiply[0].calls
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
