"""Regression tests for indexing large persistent collections."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.indexer import Indexer


@pytest.mark.parametrize("total", [1000, 1001])
def test_list_documents_reads_collection_in_pages(total):
    ids = [f"doc-{i}" for i in range(total)]
    documents = [f"content-{i}" for i in range(total)]
    metadatas = [{"source": f"source-{i}"} for i in range(total)]

    indexer = Indexer.__new__(Indexer)
    indexer.collection = MagicMock()

    def get_page(*, limit: int, offset: int):
        end = offset + limit
        return {
            "ids": ids[offset:end],
            "documents": documents[offset:end],
            "metadatas": metadatas[offset:end],
        }

    indexer.collection.get.side_effect = get_page

    result = indexer.list_documents(group_by_source=False)

    assert len(result) == total
    assert [doc.doc_id for doc in result] == ids
    assert indexer.collection.get.call_count == 2
    indexer.collection.get.assert_any_call(limit=1000, offset=0)
    indexer.collection.get.assert_any_call(limit=1000, offset=1000)


def test_index_command_exits_nonzero_when_indexing_fails(tmp_path):
    with patch("gptme_rag.cli.Indexer", side_effect=RuntimeError("database failure")):
        result = CliRunner().invoke(cli, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "Error indexing directory: database failure" in result.output
