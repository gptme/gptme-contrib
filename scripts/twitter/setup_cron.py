#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10,<3.12"
# dependencies = [
#   "python-crontab>=2.8.0",
#   "click>=8.0.0",
# ]
# [tool.uv]
# exclude-newer = "2024-01-01T00:00:00Z"
# ///
"""
Setup cron jobs for Twitter workflow automation.

This script sets up cron jobs for:
1. Timeline monitoring
2. Draft review notifications
3. Scheduled tweet posting

Usage:
    ./setup_twitter_cron.py install    # Install cron jobs
    ./setup_twitter_cron.py remove     # Remove cron jobs
    ./setup_twitter_cron.py status     # Show cron job status
"""

from pathlib import Path

import click
from crontab import CronItem, CronTab

# Job identifiers
MONITOR_COMMENT = "gptme_twitter_monitor"
REVIEW_COMMENT = "gptme_twitter_review_notify"
POST_COMMENT = "gptme_twitter_post"


def get_script_path(script_name: str) -> str:
    """Get absolute path to a script"""
    return str(Path(__file__).parent / script_name)


@click.group()
def cli():
    """Twitter workflow cron job manager"""
    pass


@cli.command()
@click.option("--monitor-interval", default=15, help="Monitor interval in minutes (5-60)")
@click.option(
    "--review-time",
    default="9,13,17",
    help="Daily review notification times (24h format)",
)
@click.option("--post-interval", default=30, help="Post check interval in minutes (15-60)")
def install(monitor_interval: int, review_time: str, post_interval: int) -> None:
    """Install cron jobs for Twitter workflow"""
    # Validate intervals
    monitor_interval = max(5, min(60, monitor_interval))
    post_interval = max(15, min(60, post_interval))

    # Get current user's crontab
    cron = CronTab(user=True)

    # Remove any existing jobs
    cron.remove_all(comment=MONITOR_COMMENT)
    cron.remove_all(comment=REVIEW_COMMENT)
    cron.remove_all(comment=POST_COMMENT)

    # Add monitor job
    monitor_job: CronItem = cron.new(command=f"{get_script_path('workflow.py')} monitor", comment=MONITOR_COMMENT)
    monitor_job.minute.every(monitor_interval)  # type: ignore

    # Add review notification job
    for hour in review_time.split(","):
        review_job: CronItem = cron.new(
            command="notify-send 'Twitter Review' 'Time to review tweet drafts!'",
            comment=REVIEW_COMMENT,
        )
        review_job.hour.on(int(hour))  # type: ignore
        review_job.minute.on(0)  # type: ignore

    # Add post job
    post_job: CronItem = cron.new(command=f"{get_script_path('workflow.py')} post", comment=POST_COMMENT)
    post_job.minute.every(post_interval)  # type: ignore

    # Write to crontab
    cron.write()

    click.echo("Installed cron jobs:")
    click.echo(f"- Monitor timeline every {monitor_interval} minutes")
    click.echo(f"- Review notifications at {review_time}")
    click.echo(f"- Check for posts every {post_interval} minutes")


@cli.command()
def remove():
    """Remove Twitter workflow cron jobs"""
    cron = CronTab(user=True)

    # Remove all our jobs
    cron.remove_all(comment=MONITOR_COMMENT)
    cron.remove_all(comment=REVIEW_COMMENT)
    cron.remove_all(comment=POST_COMMENT)

    # Write to crontab
    cron.write()

    click.echo("Removed all Twitter workflow cron jobs")


@cli.command()
def status():
    """Show status of Twitter workflow cron jobs"""
    cron = CronTab(user=True)

    click.echo("Twitter workflow cron jobs:")

    # Show monitor jobs
    monitor_jobs = list(cron.find_comment(MONITOR_COMMENT))
    if monitor_jobs:
        click.echo("\nMonitor jobs:")
        for job in monitor_jobs:
            click.echo(f"  {job}")
    else:
        click.echo("\nNo monitor jobs installed")

    # Show review notification jobs
    review_jobs = list(cron.find_comment(REVIEW_COMMENT))
    if review_jobs:
        click.echo("\nReview notification jobs:")
        for job in review_jobs:
            click.echo(f"  {job}")
    else:
        click.echo("\nNo review notification jobs installed")

    # Show post jobs
    post_jobs = list(cron.find_comment(POST_COMMENT))
    if post_jobs:
        click.echo("\nPost jobs:")
        for job in post_jobs:
            click.echo(f"  {job}")
    else:
        click.echo("\nNo post jobs installed")


if __name__ == "__main__":
    cli()
