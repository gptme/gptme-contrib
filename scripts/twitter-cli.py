#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10,<=3.11"
# dependencies = [
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
#   "tweepy>=4.14.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "click>=8.0.0",
#   "pyyaml>=6.0.0",
#   "schedule>=1.2.0",
#   "flask>=3.0.0",
# ]
# ///
"""
Twitter CLI - Command-line interface for Twitter automation.

This is a wrapper script that forwards to the CLI implementation in the twitter directory.
For full documentation, see scripts/twitter/README.md

Usage:
    ./twitter-cli.py twitter post "Hello world!"     # Post a tweet
    ./twitter-cli.py workflow monitor                # Start monitoring
"""

from twitter.cli import cli

if __name__ == "__main__":
    cli()
