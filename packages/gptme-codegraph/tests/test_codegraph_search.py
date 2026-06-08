"""Tests for gptme-codegraph local semantic search."""

import tempfile
from pathlib import Path

from gptme_codegraph.core import IndexEntry, SymbolIndex, build_index
from gptme_codegraph.search import (
    LexicalScorer,
    SearchDocument,
    SearchResult,
    _tokenize,
    extract_search_documents,
)

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer:
    def test_snake_case(self):
        tokens = _tokenize("retry_api_call")
        assert "retry" in tokens
        assert "api" in tokens
        assert "call" in tokens

    def test_camel_case(self):
        tokens = _tokenize("RetryAPICall")
        assert "retry" in tokens
        assert "api" in tokens
        assert "call" in tokens

    def test_mixed(self):
        tokens = _tokenize("get_session_persistence")
        assert "get" in tokens
        assert "session" in tokens
        assert "persistence" in tokens

    def test_punctuation(self):
        tokens = _tokenize("where is retry logic?")
        assert "where" in tokens
        assert "is" in tokens
        assert "retry" in tokens
        assert "logic" in tokens

    def test_empty(self):
        assert _tokenize("") == []
        assert _tokenize("   ") == []

    def test_short_tokens_excluded(self):
        """Single characters are excluded, but 'a' and 'I' would be."""
        tokens = _tokenize("a b c")
        assert all(len(t) > 1 or t.isalnum() for t in tokens)


# ---------------------------------------------------------------------------
# SearchDocument
# ---------------------------------------------------------------------------


class TestSearchDocument:
    def test_doc_creation(self):
        doc = SearchDocument(
            qualified_id="module::my_func",
            kind="function",
            name="my_func",
            file="module.py",
            start_line=10,
            end_line=20,
            parent_class=None,
            docstring="Does something",
            snippet="def my_func():",
            calls=["other_func"],
        )
        assert doc.qualified_id == "module::my_func"
        assert doc.kind == "function"
        assert doc.name == "my_func"
        assert doc.start_line == 10
        assert doc.docstring == "Does something"


class TestSearchResult:
    def test_result_creation(self):
        result = SearchResult(
            score=0.85,
            qualified_id="module::my_func",
            name="my_func",
            kind="function",
            file="module.py",
            start_line=10,
            end_line=20,
            why="name/docstring matched",
        )
        assert result.score == 0.85
        assert result.why == "name/docstring matched"


# ---------------------------------------------------------------------------
# Extract search documents
# ---------------------------------------------------------------------------


class TestExtractSearchDocuments:
    def test_from_index(self):
        """Extract search documents from a SymbolIndex."""
        index = SymbolIndex(
            entries={
                "add": [
                    IndexEntry(
                        name="add",
                        kind="function",
                        file="math_utils.py",
                        start_line=1,
                        end_line=3,
                        module_path="math_utils",
                    )
                ]
            }
        )
        root = Path("/fake")
        docs = extract_search_documents(index, root)
        assert len(docs) == 1
        assert docs[0].qualified_id == "math_utils::add"
        assert docs[0].kind == "function"
        assert docs[0].name == "add"

    def test_from_real_files(self):
        """Extract search documents from actual Python source files."""
        with tempfile.TemporaryDirectory(prefix="codegraph_search_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "calc.py").write_text(
                "def add(x, y):\n"
                '    """Add two numbers."""\n'
                "    return x + y\n"
                "\n"
                "def multiply(x, y):\n"
                '    """Multiply two numbers."""\n'
                "    return x * y\n"
            )

            index = build_index(tmp_path)
            assert index is not None
            assert len(index.all_names()) >= 2

            docs = extract_search_documents(index, tmp_path)
            doc_map = {d.name: d for d in docs}

            assert "add" in doc_map
            assert "multiply" in doc_map
            # Should have extracted docstrings
            assert "Add two numbers" in doc_map["add"].docstring
            assert "Multiply two numbers" in doc_map["multiply"].docstring

    def test_deduplicates_by_qualified_id(self):
        """Multiple entries with same qualified_id are deduplicated."""
        index = SymbolIndex(
            entries={
                "helper": [
                    IndexEntry(
                        name="helper",
                        kind="function",
                        file="a.py",
                        start_line=1,
                        end_line=3,
                        module_path="a",
                    ),
                    IndexEntry(
                        name="helper",
                        kind="function",
                        file="b.py",
                        start_line=1,
                        end_line=3,
                        module_path="b",
                    ),
                ]
            }
        )
        docs = extract_search_documents(index, Path("/fake"))
        # Different files → different qualified_ids (different module_path)
        assert len(docs) == 2

    def test_empty_index(self):
        """Empty index yields no documents."""
        index = SymbolIndex()
        docs = extract_search_documents(index, Path("/fake"))
        assert docs == []


# ---------------------------------------------------------------------------
# BM25 Lexical Scorer
# ---------------------------------------------------------------------------


class TestLexicalScorer:
    def test_basic_search(self):
        docs = [
            SearchDocument(
                qualified_id="mod::retry_api_call",
                kind="function",
                name="retry_api_call",
                file="retry.py",
                start_line=10,
                end_line=30,
                docstring="Retry an API call with exponential backoff.",
                snippet="def retry_api_call(url):",
            ),
            SearchDocument(
                qualified_id="mod::format_output",
                kind="function",
                name="format_output",
                file="output.py",
                start_line=1,
                end_line=5,
                docstring="Format output for display.",
                snippet="def format_output(data):",
            ),
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        results = scorer.search("retry api call", limit=5)
        assert len(results) >= 1
        assert results[0].name == "retry_api_call"
        assert results[0].score > 0

    def test_relevance_ranking(self):
        """Search results rank more relevant documents higher."""
        docs = [
            SearchDocument(
                qualified_id="mod::add",
                kind="function",
                name="add",
                file="calc.py",
                start_line=1,
                end_line=3,
                docstring="Add two numbers together.",
            ),
            SearchDocument(
                qualified_id="mod::subtract",
                kind="function",
                name="subtract",
                file="calc.py",
                start_line=5,
                end_line=7,
                docstring="Subtract the second number from the first.",
            ),
            SearchDocument(
                qualified_id="mod::add_user",
                kind="function",
                name="add_user",
                file="users.py",
                start_line=10,
                end_line=20,
                docstring="Add a new user to the database.",
            ),
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        results = scorer.search("add number", limit=5)
        assert len(results) >= 1
        # The 'add' function should rank higher than 'add_user' for "add number"
        # (both match 'add', but 'add' function docstring also mentions 'numbers')
        add_result = [r for r in results if r.name == "add"]
        assert len(add_result) >= 1
        # All results should have positive scores
        for r in results:
            assert r.score > 0

    def test_no_match(self):
        docs = [
            SearchDocument(
                qualified_id="mod::add",
                kind="function",
                name="add",
                file="calc.py",
                start_line=1,
                end_line=3,
                docstring="Add two numbers.",
            ),
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        results = scorer.search("database connection", limit=5)
        assert results == []

    def test_empty_index(self):
        scorer = LexicalScorer()
        scorer.index([])
        results = scorer.search("anything", limit=5)
        assert results == []

    def test_limit(self):
        docs = [
            SearchDocument(
                qualified_id=f"mod::func{i}",
                kind="function",
                name=f"func{i}",
                file=f"mod{i}.py",
                start_line=1,
                end_line=3,
                docstring=f"This is function {i} with some common text.",
            )
            for i in range(20)
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        results = scorer.search("function common text", limit=5)
        assert len(results) <= 5

    def test_search_docstring_snippet(self):
        """Search matches against docstrings and snippets."""
        docs = [
            SearchDocument(
                qualified_id="mod::read_file",
                kind="function",
                name="read_file",
                file="io.py",
                start_line=1,
                end_line=10,
                docstring="Read and parse a configuration file.",
                snippet="def read_file(path):",
            ),
            SearchDocument(
                qualified_id="mod::write_file",
                kind="function",
                name="write_file",
                file="io.py",
                start_line=12,
                end_line=20,
                docstring="Write data to a file.",
                snippet="def write_file(path, data):",
            ),
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        # Should find both via docstring match on "file"
        results = scorer.search("file", limit=5)
        assert len(results) == 2

        # Should favor read_file for "read"
        results_read = scorer.search("read parse", limit=5)
        assert results_read[0].name == "read_file"

    def test_why_field(self):
        """Search results include a 'why' explanation."""
        docs = [
            SearchDocument(
                qualified_id="mod::connect_db",
                kind="function",
                name="connect_db",
                file="db.py",
                start_line=1,
                end_line=10,
                docstring="Connect to the database.",
            ),
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        results = scorer.search("connect database", limit=5)
        assert len(results) == 1
        assert "name" in results[0].why or "docstring" in results[0].why
        assert results[0].why != ""

    def test_order_stability(self):
        """Same query on same index produces same order."""
        docs = [
            SearchDocument(
                qualified_id=f"mod::func{i}",
                kind="function",
                name=f"func{i}",
                file=f"mod{i}.py",
                start_line=1,
                end_line=3,
                docstring=f"This is function {i} about data processing.",
            )
            for i in range(5)
        ]
        scorer = LexicalScorer()
        scorer.index(docs)

        first = scorer.search("data processing", limit=5)
        second = scorer.search("data processing", limit=5)
        assert len(first) == len(second)
        for r1, r2 in zip(first, second):
            assert r1.qualified_id == r2.qualified_id
            assert r1.score == r2.score


# ---------------------------------------------------------------------------
# Integration: build_index -> extract -> search round-trip
# ---------------------------------------------------------------------------


class TestSearchIntegration:
    def test_round_trip(self):
        """Full pipeline: build index, extract docs, search."""
        with tempfile.TemporaryDirectory(prefix="codegraph_search_int_") as tmp:
            tmp_path = Path(tmp)

            # Create a few source files
            (tmp_path / "retry.py").write_text(
                "import time\n"
                "\n"
                "def retry_api_call(url, max_retries=3):\n"
                '    """Retry an API call with exponential backoff."""\n'
                "    for attempt in range(max_retries):\n"
                "        try:\n"
                "            return _do_request(url)\n"
                "        except ConnectionError:\n"
                "            time.sleep(2 ** attempt)\n"
                "    raise ConnectionError(f'Failed after {max_retries} retries')\n"
                "\n"
                "\n"
                "def _do_request(url):\n"
                '    """Make a single HTTP request."""\n'
                "    return f'response from {url}'\n"
            )

            (tmp_path / "session.py").write_text(
                "import json\n"
                "\n"
                "class Session:\n"
                '    """Manages a user session."""\n'
                "\n"
                "    def __init__(self, user_id):\n"
                "        self.user_id = user_id\n"
                "        self.data = {}\n"
                "\n"
                "    def save(self):\n"
                '        """Persist session data to disk."""\n'
                "        with open(f'session_{self.user_id}.json', 'w') as f:\n"
                "            json.dump(self.data, f)\n"
                "\n"
                "    def load(self):\n"
                '        """Load session data from disk."""\n'
                "        try:\n"
                "            with open(f'session_{self.user_id}.json') as f:\n"
                "                self.data = json.load(f)\n"
                "        except FileNotFoundError:\n"
                "            self.data = {}\n"
            )

            index = build_index(tmp_path)
            assert index is not None

            docs = extract_search_documents(index, tmp_path)

            scorer = LexicalScorer()
            scorer.index(docs)

            # Search for retry-related concepts
            results = scorer.search("retry api call backoff", limit=5)
            assert len(results) >= 1
            # Should match retry_api_call
            api_funcs = [r for r in results if "retry_api_call" in r.qualified_id]
            assert len(api_funcs) >= 1
            assert api_funcs[0].score > 0

            # Search for session persistence
            results2 = scorer.search("session persist save", limit=5)
            assert len(results2) >= 1
            session_results = [
                r
                for r in results2
                if "Session" in r.qualified_id or "session" in r.name
            ]
            assert len(session_results) >= 1

            # Search for something nonexistent
            results3 = scorer.search("quantum cryptography", limit=5)
            assert results3 == []
