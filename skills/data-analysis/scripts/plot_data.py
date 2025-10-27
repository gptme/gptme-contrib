#!/usr/bin/env python3
"""
Quick data visualization helper.

Usage:
    python3 plot_data.py data.csv column1 --type hist
    python3 plot_data.py data.csv column1 column2 --type scatter
    python3 plot_data.py data.csv column --by category --type box
"""

import sys
import pandas as pd  # type: ignore
import matplotlib.pyplot as plt  # type: ignore


def plot_histogram(df, column, output=None):
    """Create histogram."""
    plt.figure(figsize=(10, 6))
    df[column].hist(bins=30, edgecolor="black")
    plt.title(f"Distribution of {column}")
    plt.xlabel(column)
    plt.ylabel("Frequency")
    plt.grid(alpha=0.3)

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Saved: {output}")
    else:
        plt.savefig(f"{column}_hist.png", dpi=150, bbox_inches="tight")
        print(f"Saved: {column}_hist.png")
    plt.close()


def plot_scatter(df, x_col, y_col, output=None):
    """Create scatter plot."""
    plt.figure(figsize=(10, 6))
    plt.scatter(df[x_col], df[y_col], alpha=0.6)
    plt.title(f"{y_col} vs {x_col}")
    plt.xlabel(x_col)
    plt.ylabel(y_col)
    plt.grid(alpha=0.3)

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Saved: {output}")
    else:
        plt.savefig(f"{x_col}_vs_{y_col}_scatter.png", dpi=150, bbox_inches="tight")
        print(f"Saved: {x_col}_vs_{y_col}_scatter.png")
    plt.close()


def plot_boxplot(df, column, by_column, output=None):
    """Create box plot grouped by category."""
    plt.figure(figsize=(12, 6))
    df.boxplot(column=column, by=by_column)
    plt.title(f"{column} by {by_column}")
    plt.suptitle("")  # Remove default title
    plt.xlabel(by_column)
    plt.ylabel(column)

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Saved: {output}")
    else:
        plt.savefig(f"{column}_by_{by_column}_box.png", dpi=150, bbox_inches="tight")
        print(f"Saved: {column}_by_{by_column}_box.png")
    plt.close()


def plot_timeseries(df, date_col, value_col, output=None):
    """Create time series plot."""
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col)

    plt.figure(figsize=(14, 6))
    plt.plot(df[date_col], df[value_col])
    plt.title(f"{value_col} over time")
    plt.xlabel(date_col)
    plt.ylabel(value_col)
    plt.xticks(rotation=45)
    plt.grid(alpha=0.3)
    plt.tight_layout()

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Saved: {output}")
    else:
        plt.savefig(f"{value_col}_timeseries.png", dpi=150, bbox_inches="tight")
        print(f"Saved: {value_col}_timeseries.png")
    plt.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:")
        print("  Histogram:    python3 plot_data.py data.csv column --type hist")
        print("  Scatter:      python3 plot_data.py data.csv col1 col2 --type scatter")
        print("  Box plot:     python3 plot_data.py data.csv column --by category --type box")
        print("  Time series:  python3 plot_data.py data.csv date value --type time")
        sys.exit(1)

    filepath = sys.argv[1]
    df = pd.read_csv(filepath)
    print(f"Loaded: {filepath} ({df.shape[0]} rows)")

    # Parse plot type
    plot_type = "hist"  # default
    if "--type" in sys.argv:
        type_idx = sys.argv.index("--type")
        plot_type = sys.argv[type_idx + 1]

    # Execute based on type
    if plot_type == "hist":
        column = sys.argv[2]
        plot_histogram(df, column)

    elif plot_type == "scatter":
        x_col = sys.argv[2]
        y_col = sys.argv[3]
        plot_scatter(df, x_col, y_col)

    elif plot_type == "box":
        column = sys.argv[2]
        by_idx = sys.argv.index("--by")
        by_column = sys.argv[by_idx + 1]
        plot_boxplot(df, column, by_column)

    elif plot_type == "time":
        date_col = sys.argv[2]
        value_col = sys.argv[3]
        plot_timeseries(df, date_col, value_col)

    else:
        print(f"Unknown plot type: {plot_type}")
        print("Supported: hist, scatter, box, time")
        sys.exit(1)
