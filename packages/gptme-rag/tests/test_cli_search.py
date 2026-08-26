"""Tests for the CLI search command, focusing on --json flag."""

import json
import logging
import subprocess
import sys

import pytest
from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.document import Document
from gptme_rag.indexing.indexer import Indexer


@pytest.fixture
def populated_index(tmp_path):
    """Create and populate a temporary index for CLI testing."""
    indexer = Indexer(
        persist_directory=tmp_path / "index",
        enable_persist=True,
        chunk_size=200,
        chunk_overlap=20,
    )
    docs = [
        Document(
            content="Python is a high-level programming language known for readability.",
            metadata={"source": str(tmp_path / "python.txt"), "extension": ".txt"},
            doc_id="python",
        ),
        Document(
            content="Machine learning uses statistical methods to learn from data.",
            metadata={"source": str(tmp_path / "ml.txt"), "extension": ".txt"},
            doc_id="ml",
        ),
    ]
    indexer.add_documents(docs)
    return tmp_path / "index"


def test_search_json_output_structure(populated_index):
    """JSON output has the required top-level keys."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "Python programming",
            "--persist-dir",
            str(populated_index),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "query" in data
    assert "results" in data
    assert "total_results" in data
    assert "context" in data


def test_search_json_output_query_echoed(populated_index):
    """The query is echoed back in JSON output."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "Python programming",
            "--persist-dir",
            str(populated_index),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["query"] == "Python programming"


def test_search_json_output_result_fields(populated_index):
    """Each result has source, relevance, content, and metadata fields."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "Python",
            "--persist-dir",
            str(populated_index),
            "--n-results",
            "1",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total_results"] >= 1
    first = data["results"][0]
    assert "source" in first
    assert "relevance" in first
    assert "content" in first
    assert "metadata" in first
    # relevance is a float in [0, 1]
    assert isinstance(first["relevance"], float)
    assert 0.0 <= first["relevance"] <= 1.0


def test_search_json_output_context_info(populated_index):
    """Context info block includes total_tokens, included_results, and truncated."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "machine learning",
            "--persist-dir",
            str(populated_index),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ctx = data["context"]
    assert "total_tokens" in ctx
    assert "truncated" in ctx
    assert "results_in_context" in ctx
    assert isinstance(ctx["total_tokens"], int)
    assert isinstance(ctx["truncated"], bool)
    assert isinstance(ctx["results_in_context"], int)
    # results_in_context <= total_results (equals when not truncated)
    assert ctx["results_in_context"] <= data["total_results"]
    if not ctx["truncated"]:
        assert ctx["results_in_context"] == data["total_results"]


def test_search_json_no_results(tmp_path):
    """JSON output for an empty index returns empty results list with context key."""
    # Create the index directory (CLI requires it to exist)
    Indexer(
        persist_directory=tmp_path / "empty",
        enable_persist=True,
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "anything",
            "--persist-dir",
            str(tmp_path / "empty"),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["results"] == []
    assert data["total_results"] == 0
    # context key must be present even when there are no results (schema consistency)
    assert "context" in data
    assert data["context"]["total_tokens"] == 0
    assert data["context"]["truncated"] is False
    assert data["context"]["results_in_context"] == 0


def test_search_json_format_flag_warns(populated_index):
    """--format is silently ignored when --json is set, with a stderr warning."""
    # Use subprocess so we can capture real stderr separately from stdout
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_rag.cli",
            "search",
            "Python",
            "--persist-dir",
            str(populated_index),
            "--json",
            "--format",
            "full",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # stdout should be valid JSON
    data = json.loads(result.stdout)
    assert "results" in data
    # Warning should appear on stderr
    assert "Warning" in result.stderr
    assert "--format" in result.stderr


def test_search_json_output_is_valid_json(populated_index):
    """Output is valid JSON (no rich markup, no extra text)."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "data",
            "--persist-dir",
            str(populated_index),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Should parse without error
    data = json.loads(result.output)
    assert isinstance(data, dict)


def test_search_json_expand_truncated_consistency(populated_index):
    """When --expand is used, truncated is always False (all results are returned)."""
    runner = CliRunner()
    for expand_mode in ("adjacent", "file"):
        result = runner.invoke(
            cli,
            [
                "search",
                "Python",
                "--persist-dir",
                str(populated_index),
                "--json",
                "--expand",
                expand_mode,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ctx = data["context"]
        # truncated must be False when expand is active — all results are returned
        assert ctx["truncated"] is False, (
            f"--expand {expand_mode}: truncated={ctx['truncated']} but "
            f"results_in_context={ctx['results_in_context']} == total_results={data['total_results']}"
        )
        # results_in_context must equal total_results in expand mode
        assert ctx["results_in_context"] == data["total_results"]


def test_search_json_truncated_on_dedup(tmp_path):
    """When assemble_context deduplicates chunks, truncated reflects the drop."""
    index_dir = tmp_path / "dedup_index"
    indexer = Indexer(
        persist_directory=index_dir,
        enable_persist=True,
        # Large chunk size to avoid splitting — each doc becomes one chunk
        chunk_size=500,
        chunk_overlap=0,
    )
    # Two documents with identical content but different sources
    shared_content = "Duplicate content about Python programming and data analysis."
    docs = [
        Document(
            content=shared_content,
            metadata={"source": str(tmp_path / "file_a.txt"), "extension": ".txt"},
            doc_id="dup_a",
        ),
        Document(
            content=shared_content,
            metadata={"source": str(tmp_path / "file_b.txt"), "extension": ".txt"},
            doc_id="dup_b",
        ),
    ]
    indexer.add_documents(docs)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "Python programming",
            "--persist-dir",
            str(index_dir),
            "--n-results",
            "10",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ctx = data["context"]
    # Both docs have distinct IDs (dup_a, dup_b) so ChromaDB stores and returns
    # both — the indexer deduplicates by ID only, not by content.
    assert data["total_results"] == 2, (
        f"Expected both duplicate docs to be returned by ChromaDB, "
        f"got total_results={data['total_results']}"
    )
    # assemble_context sees identical content and drops the second doc
    assert ctx["results_in_context"] == 1, (
        f"Expected dedup to keep only 1 doc, got results_in_context={ctx['results_in_context']}"
    )
    # The fix: truncated must be True when dedup dropped results
    assert ctx["truncated"] is True, (
        f"results_in_context={ctx['results_in_context']} < "
        f"total_results={data['total_results']} but truncated=False"
    )


def test_search_human_format_is_default(populated_index):
    """Default output is human-readable (not JSON)."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["search", "Python", "--persist-dir", str(populated_index)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Human output should NOT be valid JSON at the top level
    try:
        json.loads(result.output)
        is_json = True
    except json.JSONDecodeError:
        is_json = False
    assert not is_json, "Default output should be human-readable, not JSON"


def test_search_json_emits_error_object_on_init_failure(tmp_path):
    """In --json mode, an Indexer init failure produces a JSON error object on stdout.

    Regression guard for the silent-failure bug: before the fix, exceptions inside
    the redirect_stderr(devnull) block would propagate with no stdout output,
    leaving callers with an empty stdout and a non-zero exit code and no way to
    diagnose the failure.
    """
    from unittest.mock import patch as mock_patch

    runner = CliRunner()
    with mock_patch(
        "gptme_rag.cli.Indexer",
        side_effect=RuntimeError("model weights not found"),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--persist-dir",
                str(tmp_path / "index"),
                "--json",
            ],
        )

    # Must exit non-zero (exception re-raised after emitting JSON)
    assert result.exit_code != 0, "Expected non-zero exit on Indexer failure"

    # Output must be non-empty and valid JSON with an 'error' key
    assert result.output.strip(), "Expected JSON error object on stdout, got empty output"
    data = json.loads(result.output.strip())
    assert "error" in data, f"Expected 'error' key in JSON output, got: {data}"
    assert "model weights not found" in data["error"]
    assert data.get("query") == "test query"


def test_search_json_emits_error_object_on_search_failure(tmp_path):
    """In --json mode, a search failure also produces a JSON error object.

    Regression guard for failures after Indexer initialization succeeds: the
    search() call itself must stay inside the exception-to-JSON wrapper.
    """
    from unittest.mock import Mock, patch as mock_patch

    failing_indexer = Mock()
    failing_indexer.search.side_effect = RuntimeError("vector store unavailable")

    runner = CliRunner()
    with mock_patch("gptme_rag.cli.Indexer", return_value=failing_indexer):
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--persist-dir",
                str(tmp_path / "index"),
                "--json",
            ],
        )

    assert result.exit_code != 0, "Expected non-zero exit on search failure"
    assert result.output.strip(), "Expected JSON error object on stdout, got empty output"

    data = json.loads(result.output.strip())
    assert "error" in data, f"Expected 'error' key in JSON output, got: {data}"
    assert "vector store unavailable" in data["error"]
    assert data.get("query") == "test query"


def test_search_json_emits_error_object_on_invalid_weights(tmp_path):
    """In --json mode, invalid --weights JSON produces a JSON error object on stdout.

    Regression guard: before the fix, the error went to console (stderr/devnull in
    json mode) and the function returned with exit 0 and empty stdout.  JSON consumers
    could not distinguish this from a successful empty-result response.
    """
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "search",
            "test query",
            "--persist-dir",
            str(tmp_path / "index"),
            "--json",
            "--weights",
            "not-valid-json{{{",
        ],
    )

    # Must exit cleanly (weights error is a user error, not a crash)
    # stdout must be non-empty and valid JSON with an 'error' key
    assert result.output.strip(), "Expected JSON error object on stdout, got empty output"
    data = json.loads(result.output.strip())
    assert "error" in data, f"Expected 'error' key in JSON output, got: {data}"
    assert "weights" in data["error"].lower() or "json" in data["error"].lower(), (
        f"Expected error to mention weights/JSON, got: {data['error']}"
    )


def test_search_json_emits_error_object_on_get_expanded_content_failure(tmp_path):
    """In --json mode, a get_expanded_content failure produces a JSON error object.

    Regression guard: before the fix, the formatting loop (after the main try/except)
    could raise without emitting anything to stdout.  JSON consumers received empty
    stdout plus a non-zero exit code with no way to diagnose the failure.
    """
    from unittest.mock import Mock, patch as mock_patch

    from gptme_rag.indexing.document import Document

    fake_doc = Document(content="hello world", metadata={"source": "test.txt"}, doc_id="doc1")
    mock_indexer = Mock()
    mock_indexer.search.return_value = ([fake_doc], [0.9], None)

    mock_assembler = Mock()
    assembled = Mock()
    assembled.documents = [fake_doc]
    assembled.truncated = False
    assembled.total_tokens = 10
    mock_assembler.assemble_context.return_value = assembled
    mock_assembler.count_tokens.return_value = 5

    runner = CliRunner()
    with (
        mock_patch("gptme_rag.cli.Indexer", return_value=mock_indexer),
        mock_patch("gptme_rag.cli.ContextAssembler", return_value=mock_assembler),
        mock_patch(
            "gptme_rag.cli.ChunkMerger.get_adjacent_chunks",
            side_effect=RuntimeError("corrupted index entry"),
        ),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--persist-dir",
                str(tmp_path / "index"),
                "--json",
                "--expand",
                "adjacent",
            ],
        )

    assert result.exit_code != 0, "Expected non-zero exit on get_expanded_content failure"
    assert result.output.strip(), "Expected JSON error object on stdout, got empty output"
    data = json.loads(result.output.strip())
    assert "error" in data, f"Expected 'error' key in JSON output, got: {data}"
    assert "corrupted index entry" in data["error"]
    assert data.get("query") == "test query"


def test_search_json_emits_error_object_on_assemble_context_failure(tmp_path):
    """In --json mode, an assemble_context failure also produces a JSON error object.

    Regression guard for failures after Indexer.search() succeeds: assemble_context
    runs inside the same try/except block, so its exceptions must also be caught
    and emitted as JSON rather than silently discarded.
    """
    from unittest.mock import Mock, patch as mock_patch

    from gptme_rag.indexing.document import Document

    fake_doc = Document(content="hello world", metadata={"source": "test.txt"}, doc_id="doc1")

    failing_assembler = Mock()
    failing_assembler.assemble_context.side_effect = RuntimeError("assembler index error")

    # Indexer succeeds and returns a document; ContextAssembler fails on assemble_context
    mock_indexer = Mock()
    mock_indexer.search.return_value = ([fake_doc], [0.9], None)

    runner = CliRunner()
    with (
        mock_patch("gptme_rag.cli.Indexer", return_value=mock_indexer),
        mock_patch("gptme_rag.cli.ContextAssembler", return_value=failing_assembler),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--persist-dir",
                str(tmp_path / "index"),
                "--json",
            ],
        )

    assert result.exit_code != 0, "Expected non-zero exit on assemble_context failure"
    assert result.output.strip(), "Expected JSON error object on stdout, got empty output"

    data = json.loads(result.output.strip())
    assert "error" in data, f"Expected 'error' key in JSON output, got: {data}"
    assert "assembler index error" in data["error"]
    assert data.get("query") == "test query"


def test_search_json_surfaces_warning_only_degraded_path(tmp_path):
    """Warnings emitted inside the json-mode redirect block are kept in JSON output."""
    from unittest.mock import Mock, patch as mock_patch

    fake_doc = Document(content="hello world", metadata={"source": "test.txt"}, doc_id="doc1")

    mock_indexer = Mock()
    mock_indexer.search.return_value = ([fake_doc], [0.1], None)

    assembled = Mock()
    assembled.documents = [fake_doc]
    assembled.truncated = False
    mock_assembler = Mock()
    mock_assembler.assemble_context.return_value = assembled
    mock_assembler.count_tokens.return_value = 2

    def make_indexer(*args, **kwargs):
        logging.getLogger("gptme_rag.indexing.indexer").warning(
            "Embedding model mismatch; continuing with stored embedding function"
        )
        return mock_indexer

    runner = CliRunner()
    with (
        mock_patch("gptme_rag.cli.Indexer", side_effect=make_indexer),
        mock_patch("gptme_rag.cli.ContextAssembler", return_value=mock_assembler),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--persist-dir",
                str(tmp_path / "index"),
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["warnings"] == [
        "Embedding model mismatch; continuing with stored embedding function"
    ]


def test_search_invalid_weights_does_not_leak_warning_handler(tmp_path):
    """An early return during weights parsing must not mutate root logging."""
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)

    result = CliRunner().invoke(
        cli,
        [
            "search",
            "test query",
            "--persist-dir",
            str(tmp_path / "index"),
            "--weights",
            "not-valid-json{{{",
        ],
    )

    assert result.exit_code == 0, result.output
    assert root_logger.handlers == handlers_before


def test_search_non_json_surfaces_warning_to_human(tmp_path):
    """Non-json search must show arbitrary warning text without Rich parsing it.

    stdout is redirected to /dev/null during init/search in *both* modes, and
    RichHandler logs to stdout — so without an explicit re-emit the warning is
    swallowed and a human sees a healthy-looking result set.
    """
    from unittest.mock import Mock, patch as mock_patch

    fake_doc = Document(content="hello world", metadata={"source": "test.txt"}, doc_id="doc1")

    mock_indexer = Mock()
    mock_indexer.search.return_value = ([fake_doc], [0.1], None)

    assembled = Mock()
    assembled.documents = [fake_doc]
    assembled.truncated = False
    mock_assembler = Mock()
    mock_assembler.assemble_context.return_value = assembled
    mock_assembler.count_tokens.return_value = 2
    warning = "Embedding model mismatch for [bert-base]; continuing with stored model ["

    def make_indexer(*args, **kwargs):
        logging.getLogger("gptme_rag.indexing.indexer").warning(warning)
        return mock_indexer

    runner = CliRunner()
    with (
        mock_patch("gptme_rag.cli.Indexer", side_effect=make_indexer),
        mock_patch("gptme_rag.cli.ContextAssembler", return_value=mock_assembler),
    ):
        result = runner.invoke(
            cli,
            ["search", "test query", "--persist-dir", str(tmp_path / "index")],
        )

    assert result.exit_code == 0, result.output
    assert warning in result.output


def test_search_non_json_surfaces_warning_before_failure(tmp_path):
    """A captured warning must remain visible when search later fails."""
    from unittest.mock import patch as mock_patch

    warning = "Embedding model mismatch; continuing with stored model"

    class FailingIndexer:
        def __init__(self, *args, **kwargs):
            logging.getLogger("gptme_rag.indexing.indexer").warning(warning)

        def search(self, *args, **kwargs):
            raise RuntimeError("embedding dimension mismatch")

    runner = CliRunner()
    with mock_patch("gptme_rag.cli.Indexer", FailingIndexer):
        result = runner.invoke(
            cli,
            ["search", "test query", "--persist-dir", str(tmp_path / "index")],
        )

    assert result.exit_code != 0
    assert warning in result.output
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "embedding dimension mismatch"
