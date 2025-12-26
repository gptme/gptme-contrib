"""Tests for gptme-wrapped plugin."""

import json
from pathlib import Path

import pytest

from gptme_wrapped.tools import wrapped_stats, wrapped_report, wrapped_export


def create_test_conversation(conv_dir: Path, messages: list[dict]) -> None:
    """Create a test conversation directory with messages."""
    conv_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = conv_dir / "conversation.jsonl"
    with open(jsonl_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


class TestWrappedStats:
    """Tests for wrapped_stats function."""

    def test_empty_logs_dir(self, tmp_path: Path):
        """Test with empty logs directory."""
        stats = wrapped_stats(2025, logs_dir=tmp_path)
        assert stats["conversations"] == 0
        assert stats["messages"] == 0
        assert stats["cost"] == 0.0

    def test_single_conversation(self, tmp_path: Path):
        """Test with a single conversation."""
        conv_dir = tmp_path / "2025-12-25-test-conv"
        messages = [
            {
                "role": "user",
                "content": "Hello",
                "timestamp": "2025-12-25T10:00:00",
            },
            {
                "role": "assistant",
                "content": "Hi there!",
                "timestamp": "2025-12-25T10:00:05",
                "metadata": {
                    "model": "test-model",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 80,
                    "cost": 0.01,
                },
            },
        ]
        create_test_conversation(conv_dir, messages)
        
        stats = wrapped_stats(2025, logs_dir=tmp_path)
        assert stats["conversations"] == 1
        assert stats["messages"] == 2
        assert stats["input_tokens"] == 100
        assert stats["output_tokens"] == 50
        assert stats["cache_read_tokens"] == 80
        assert stats["cost"] == 0.01
        assert "test-model" in stats["models"]

    def test_year_filtering(self, tmp_path: Path):
        """Test that conversations are filtered by year."""
        # 2024 conversation
        conv_2024 = tmp_path / "2024-12-25-old-conv"
        create_test_conversation(conv_2024, [
            {"role": "user", "content": "Old", "timestamp": "2024-12-25T10:00:00"},
        ])
        
        # 2025 conversation
        conv_2025 = tmp_path / "2025-01-01-new-conv"
        create_test_conversation(conv_2025, [
            {"role": "user", "content": "New", "timestamp": "2025-01-01T10:00:00"},
        ])
        
        stats_2025 = wrapped_stats(2025, logs_dir=tmp_path)
        assert stats_2025["conversations"] == 1
        
        stats_2024 = wrapped_stats(2024, logs_dir=tmp_path)
        assert stats_2024["conversations"] == 1


class TestWrappedReport:
    """Tests for wrapped_report function."""

    def test_report_format(self, tmp_path: Path):
        """Test that report is properly formatted."""
        conv_dir = tmp_path / "2025-12-25-test"
        messages = [
            {
                "role": "assistant",
                "content": "Test",
                "timestamp": "2025-12-25T14:30:00",
                "metadata": {
                    "model": "claude-sonnet",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cost": 0.05,
                },
            },
        ]
        create_test_conversation(conv_dir, messages)
        
        report = wrapped_report(2025, logs_dir=tmp_path)
        assert "gptme Wrapped 2025" in report
        assert "conversations" in report.lower()
        assert "tokens" in report.lower()


class TestWrappedExport:
    """Tests for wrapped_export function."""

    def test_json_export(self, tmp_path: Path):
        """Test JSON export format."""
        result = wrapped_export(2025, format="json", logs_dir=tmp_path)
        data = json.loads(result)
        assert "year" in data
        assert data["year"] == 2025

    def test_csv_export(self, tmp_path: Path):
        """Test CSV export format."""
        result = wrapped_export(2025, format="csv", logs_dir=tmp_path)
        assert "metric,value" in result
        assert "year,2025" in result

    def test_html_export(self, tmp_path: Path):
        """Test HTML export format."""
        result = wrapped_export(2025, format="html", logs_dir=tmp_path)
        assert "<!DOCTYPE html>" in result
        assert "gptme Wrapped 2025" in result
