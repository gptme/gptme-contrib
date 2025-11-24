"""
Cost tracking for image generation operations.

Tracks generation costs across providers to help users monitor spending.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Provider pricing (USD per image, approximate as of Nov 2024)
PROVIDER_COSTS = {
    "gemini": {
        "imagen-3-fast": 0.04,  # Standard generation
        "imagen-3": 0.08,  # Higher quality
    },
    "dalle": {
        "standard": 0.04,  # 1024x1024 standard
        "hd": 0.08,  # 1024x1024 HD
    },
    "dalle2": {
        "standard": 0.02,  # 1024x1024
    },
}


class CostTracker:
    """Track and query image generation costs."""

    def __init__(self, db_path: str | Path = "~/.gptme/imagen_costs.db"):
        """Initialize cost tracker with database path."""
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    prompt TEXT NOT NULL,
                    size TEXT,
                    quality TEXT,
                    count INTEGER DEFAULT 1,
                    cost_usd REAL NOT NULL,
                    output_path TEXT
                )
            """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON generations(timestamp)
            """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider
                ON generations(provider)
            """
            )

    def calculate_cost(
        self,
        provider: str,
        quality: str = "standard",
        count: int = 1,
        model: str | None = None,
    ) -> float:
        """
        Calculate cost for image generation.

        Args:
            provider: Provider name (gemini, dalle, dalle2)
            quality: Quality level (standard, hd)
            count: Number of images
            model: Specific model name (optional)

        Returns:
            Cost in USD
        """
        if provider not in PROVIDER_COSTS:
            return 0.0  # Unknown provider, can't estimate

        provider_prices = PROVIDER_COSTS[provider]

        # Determine cost per image
        if model and model in provider_prices:
            cost_per_image = provider_prices[model]
        elif quality in provider_prices:
            cost_per_image = provider_prices[quality]
        else:
            # Default to first available price
            cost_per_image = list(provider_prices.values())[0]

        return cost_per_image * count

    def record_generation(
        self,
        provider: str,
        prompt: str,
        cost: float,
        model: str | None = None,
        size: str | None = None,
        quality: str | None = None,
        count: int = 1,
        output_path: str | None = None,
    ) -> int:
        """
        Record a generation in the cost database.

        Args:
            provider: Provider name
            prompt: Generation prompt
            cost: Cost in USD
            model: Model name
            size: Image size
            quality: Quality level
            count: Number of images
            output_path: Path to saved image

        Returns:
            Record ID
        """
        timestamp = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO generations
                (timestamp, provider, model, prompt, size, quality, count, cost_usd, output_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    timestamp,
                    provider,
                    model,
                    prompt,
                    size,
                    quality,
                    count,
                    cost,
                    output_path,
                ),
            )
            record_id = cursor.lastrowid
            if record_id is None:
                raise RuntimeError("Failed to insert generation record")
            return record_id

    def get_total_cost(
        self,
        provider: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> float:
        """
        Get total cost, optionally filtered.

        Args:
            provider: Filter by provider (optional)
            start_date: Start date in ISO format (optional)
            end_date: End date in ISO format (optional)

        Returns:
            Total cost in USD
        """
        query = "SELECT SUM(cost_usd) FROM generations WHERE 1=1"
        params: list[Any] = []

        if provider:
            query += " AND provider = ?"
            params.append(provider)

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)

        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)

        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(query, params).fetchone()
            return result[0] or 0.0

    def get_cost_breakdown(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, float]:
        """
        Get cost breakdown by provider.

        Args:
            start_date: Start date in ISO format (optional)
            end_date: End date in ISO format (optional)

        Returns:
            Dictionary mapping provider to total cost
        """
        query = """
            SELECT provider, SUM(cost_usd)
            FROM generations
            WHERE 1=1
        """
        params: list[Any] = []

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)

        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)

        query += " GROUP BY provider"

        with sqlite3.connect(self.db_path) as conn:
            results = conn.execute(query, params).fetchall()
            return {provider: cost for provider, cost in results}

    def get_generation_history(
        self,
        limit: int = 50,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get recent generation history.

        Args:
            limit: Maximum number of records (default: 50)
            provider: Filter by provider (optional)

        Returns:
            List of generation records
        """
        query = """
            SELECT timestamp, provider, model, prompt, size, quality,
                   count, cost_usd, output_path
            FROM generations
            WHERE 1=1
        """
        params: list[Any] = []

        if provider:
            query += " AND provider = ?"
            params.append(provider)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            results = conn.execute(query, params).fetchall()
            return [dict(row) for row in results]


# Global cost tracker instance
_cost_tracker: CostTracker | None = None


def get_cost_tracker() -> CostTracker:
    """Get or create global cost tracker instance."""
    global _cost_tracker
    if _cost_tracker is None:
        _cost_tracker = CostTracker()
    return _cost_tracker
