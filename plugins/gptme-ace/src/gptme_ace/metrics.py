"""ACE Curation Quality Metrics System

Tracks and measures:
- Insight quality (before curation)
- Curation effectiveness (conversion rates)
- Lesson impact (helpfulness over time)
- System health (performance, errors)

Part of gptme-ace plugin (Phase 5: Utilities).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


@dataclass
class CurationRun:
    """Record of a curation run"""

    run_id: str
    timestamp: datetime
    trigger: str  # 'autonomous_hook', 'scheduled', 'manual'
    insights_count: int
    conversions: int
    duration_seconds: float
    tokens_used: int
    success: bool
    error_message: Optional[str] = None


@dataclass
class InsightQuality:
    """Quality metrics for a generated insight"""

    insight_id: str
    timestamp: datetime
    quality_score: float  # 0.0-1.0
    actionable: bool
    novel: bool
    category: str
    source_session: str


@dataclass
class LessonImpact:
    """Impact metrics for a curated lesson"""

    lesson_id: str
    created_timestamp: datetime
    source_insight_ids: list[str]
    total_uses: int = 0
    helpful_count: int = 0
    harmful_count: int = 0
    last_used: Optional[datetime] = None


class MetricsDB:
    """Database for ACE curation metrics"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database schema"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS curation_runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp DATETIME,
                    trigger TEXT,
                    insights_count INTEGER,
                    conversions INTEGER,
                    duration_seconds REAL,
                    tokens_used INTEGER,
                    success BOOLEAN,
                    error_message TEXT
                )
            """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS insight_quality (
                    insight_id TEXT PRIMARY KEY,
                    timestamp DATETIME,
                    quality_score REAL,
                    actionable BOOLEAN,
                    novel BOOLEAN,
                    category TEXT,
                    source_session TEXT
                )
            """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_impact (
                    lesson_id TEXT PRIMARY KEY,
                    created_timestamp DATETIME,
                    source_insight_ids TEXT,
                    total_uses INTEGER,
                    helpful_count INTEGER,
                    harmful_count INTEGER,
                    last_used DATETIME
                )
            """
            )

            # Create indexes for common queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_curation_runs_timestamp
                ON curation_runs(timestamp)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_insight_quality_timestamp
                ON insight_quality(timestamp)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lesson_impact_created
                ON lesson_impact(created_timestamp)
            """
            )

    def record_curation_run(self, run: CurationRun):
        """Record a curation run"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO curation_runs
                (run_id, timestamp, trigger, insights_count, conversions,
                 duration_seconds, tokens_used, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    run.run_id,
                    run.timestamp.isoformat(),
                    run.trigger,
                    run.insights_count,
                    run.conversions,
                    run.duration_seconds,
                    run.tokens_used,
                    run.success,
                    run.error_message,
                ),
            )

    def record_insight_quality(self, insight: InsightQuality):
        """Record insight quality metrics"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO insight_quality
                (insight_id, timestamp, quality_score, actionable, novel,
                 category, source_session)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    insight.insight_id,
                    insight.timestamp.isoformat(),
                    insight.quality_score,
                    insight.actionable,
                    insight.novel,
                    insight.category,
                    insight.source_session,
                ),
            )

    def record_lesson_impact(self, lesson: LessonImpact):
        """Record or update lesson impact metrics"""
        with sqlite3.connect(self.db_path) as conn:
            source_ids_json = json.dumps(lesson.source_insight_ids)
            last_used = lesson.last_used.isoformat() if lesson.last_used else None

            conn.execute(
                """
                INSERT OR REPLACE INTO lesson_impact
                (lesson_id, created_timestamp, source_insight_ids, total_uses,
                 helpful_count, harmful_count, last_used)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    lesson.lesson_id,
                    lesson.created_timestamp.isoformat(),
                    source_ids_json,
                    lesson.total_uses,
                    lesson.helpful_count,
                    lesson.harmful_count,
                    last_used,
                ),
            )

    def get_curation_runs(
        self, since: Optional[datetime] = None, limit: int = 100
    ) -> list[CurationRun]:
        """Get recent curation runs"""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM curation_runs"
            params: list[Any] = []

            if since:
                query += " WHERE timestamp >= ?"
                params.append(since.isoformat())

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            return [
                CurationRun(
                    run_id=row[0],
                    timestamp=datetime.fromisoformat(row[1]),
                    trigger=row[2],
                    insights_count=row[3],
                    conversions=row[4],
                    duration_seconds=row[5],
                    tokens_used=row[6],
                    success=bool(row[7]),
                    error_message=row[8],
                )
                for row in rows
            ]

    def get_insights(
        self, since: Optional[datetime] = None, limit: int = 100
    ) -> list[InsightQuality]:
        """Get recent insights"""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM insight_quality"
            params: list[Any] = []

            if since:
                query += " WHERE timestamp >= ?"
                params.append(since.isoformat())

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            return [
                InsightQuality(
                    insight_id=row[0],
                    timestamp=datetime.fromisoformat(row[1]),
                    quality_score=row[2],
                    actionable=bool(row[3]),
                    novel=bool(row[4]),
                    category=row[5],
                    source_session=row[6],
                )
                for row in rows
            ]

    def get_lessons(
        self, since: Optional[datetime] = None, limit: int = 100
    ) -> list[LessonImpact]:
        """Get recent lessons"""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM lesson_impact"
            params: list[Any] = []

            if since:
                query += " WHERE created_timestamp >= ?"
                params.append(since.isoformat())

            query += " ORDER BY created_timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            return [
                LessonImpact(
                    lesson_id=row[0],
                    created_timestamp=datetime.fromisoformat(row[1]),
                    source_insight_ids=json.loads(row[2]),
                    total_uses=row[3],
                    helpful_count=row[4],
                    harmful_count=row[5],
                    last_used=(datetime.fromisoformat(row[6]) if row[6] else None),
                )
                for row in rows
            ]


class MetricsCalculator:
    """Calculate aggregate metrics from database"""

    def __init__(self, db: MetricsDB):
        self.db = db

    def get_curation_summary(self, period: timedelta) -> dict:
        """Get curation effectiveness summary for period"""
        since = datetime.now() - period
        runs = self.db.get_curation_runs(since=since)

        if not runs:
            return {
                "period_days": period.days,
                "runs": 0,
                "success_rate": 0.0,
                "avg_duration": 0.0,
                "avg_tokens": 0,
                "total_insights": 0,
                "total_conversions": 0,
                "conversion_rate": 0.0,
            }

        successful_runs = [r for r in runs if r.success]
        total_insights = sum(r.insights_count for r in runs)
        total_conversions = sum(r.conversions for r in runs)

        return {
            "period_days": period.days,
            "runs": len(runs),
            "success_rate": len(successful_runs) / len(runs),
            "avg_duration": sum(r.duration_seconds for r in runs) / len(runs),
            "avg_tokens": sum(r.tokens_used for r in runs) / len(runs),
            "total_insights": total_insights,
            "total_conversions": total_conversions,
            "conversion_rate": (
                total_conversions / total_insights if total_insights > 0 else 0.0
            ),
        }

    def get_insight_quality_summary(self, period: timedelta) -> dict:
        """Get insight quality summary for period"""
        since = datetime.now() - period
        insights = self.db.get_insights(since=since)

        if not insights:
            return {
                "period_days": period.days,
                "total": 0,
                "avg_quality": 0.0,
                "actionable_rate": 0.0,
                "novel_rate": 0.0,
                "by_category": {},
            }

        actionable = sum(1 for i in insights if i.actionable)
        novel = sum(1 for i in insights if i.novel)

        # Count by category
        by_category: dict[str, int] = {}
        for i in insights:
            by_category[i.category] = by_category.get(i.category, 0) + 1

        return {
            "period_days": period.days,
            "total": len(insights),
            "avg_quality": sum(i.quality_score for i in insights) / len(insights),
            "actionable_rate": actionable / len(insights),
            "novel_rate": novel / len(insights),
            "by_category": by_category,
        }

    def get_lesson_impact_summary(self, period: timedelta) -> dict:
        """Get lesson impact summary for period"""
        since = datetime.now() - period
        lessons = self.db.get_lessons(since=since)

        if not lessons:
            return {
                "period_days": period.days,
                "created": 0,
                "avg_uses": 0.0,
                "avg_helpful_ratio": 0.0,
                "avg_effectiveness": 0.0,
            }

        def helpful_ratio(lesson: LessonImpact) -> float:
            total = lesson.helpful_count + lesson.harmful_count
            return lesson.helpful_count / total if total > 0 else 0.0

        def effectiveness(lesson: LessonImpact) -> float:
            """Combined metric: helpfulness × usage"""
            return helpful_ratio(lesson) * min(lesson.total_uses / 10.0, 1.0)

        return {
            "period_days": period.days,
            "created": len(lessons),
            "avg_uses": sum(lesson.total_uses for lesson in lessons) / len(lessons),
            "avg_helpful_ratio": sum(helpful_ratio(lesson) for lesson in lessons)
            / len(lessons),
            "avg_effectiveness": sum(effectiveness(lesson) for lesson in lessons)
            / len(lessons),
        }

    def get_system_health(self) -> dict:
        """Get overall system health status"""
        # Last 7 days metrics
        week = timedelta(days=7)
        curation = self.get_curation_summary(week)
        insights = self.get_insight_quality_summary(week)
        lessons = self.get_lesson_impact_summary(week)

        # Health thresholds
        health_status = "healthy"
        alerts = []

        if curation["success_rate"] < 0.8:
            health_status = "warning"
            alerts.append(
                f"Low success rate: {curation['success_rate']:.1%} (target: 80%)"
            )

        if curation["conversion_rate"] < 0.5:
            health_status = "warning"
            alerts.append(
                f"Low conversion rate: {curation['conversion_rate']:.1%} (target: 50%)"
            )

        if insights["avg_quality"] < 0.6:
            health_status = "warning"
            alerts.append(
                f"Low insight quality: {insights['avg_quality']:.2f} (target: 0.6)"
            )

        if lessons["avg_helpful_ratio"] < 0.7:
            health_status = "warning"
            alerts.append(
                f"Low helpful ratio: {lessons['avg_helpful_ratio']:.1%} (target: 70%)"
            )

        return {
            "status": health_status,
            "alerts": alerts,
            "curation": curation,
            "insights": insights,
            "lessons": lessons,
        }


def get_default_metrics_db(workspace: Optional[Path] = None) -> MetricsDB:
    """Get default metrics database instance.

    Args:
        workspace: Optional workspace path. If not provided, uses current directory.

    Returns:
        MetricsDB instance configured for the workspace.
    """
    if workspace is None:
        # Default to current working directory
        workspace = Path.cwd()

    db_path = workspace / "logs" / "ace_curation_metrics.db"
    return MetricsDB(db_path)


if __name__ == "__main__":
    # CLI for quick metrics inspection
    import sys

    if len(sys.argv) > 1:
        workspace = Path(sys.argv[1])
    else:
        workspace = Path.cwd()

    db = get_default_metrics_db(workspace)
    calc = MetricsCalculator(db)

    print("=== ACE System Health ===")
    health = calc.get_system_health()
    print(f"Status: {health['status']}")

    if health["alerts"]:
        print("\nAlerts:")
        for alert in health["alerts"]:
            print(f"  ⚠️  {alert}")

    print("\nCuration (7 days):")
    print(f"  Runs: {health['curation']['runs']}")
    print(f"  Success Rate: {health['curation']['success_rate']:.1%}")
    print(f"  Conversion Rate: {health['curation']['conversion_rate']:.1%}")

    print("\nInsights (7 days):")
    print(f"  Total: {health['insights']['total']}")
    print(f"  Avg Quality: {health['insights']['avg_quality']:.2f}")

    print("\nLessons (7 days):")
    print(f"  Created: {health['lessons']['created']}")
    print(f"  Avg Effectiveness: {health['lessons']['avg_effectiveness']:.2f}")
