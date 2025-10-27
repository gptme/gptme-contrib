#!/usr/bin/env python3
"""
Quick CSV loader with automatic exploration.

Usage:
    python3 load_csv.py data.csv
    python3 load_csv.py data.csv --preview 10
"""

import sys
import pandas as pd  # type: ignore


def load_and_explore(filepath, preview_rows=5):
    """Load CSV and print quick exploration."""
    print(f"Loading: {filepath}")
    print("-" * 60)

    # Load data
    df = pd.read_csv(filepath)

    # Basic info
    print(f"\nShape: {df.shape[0]} rows × {df.shape[1]} columns")

    # Column info
    print("\nColumns:")
    for col in df.columns:
        dtype = df[col].dtype
        null_count = df[col].isnull().sum()
        null_pct = (null_count / len(df)) * 100
        print(f"  - {col:30} {dtype:10} ({null_count} missing, {null_pct:.1f}%)")

    # Preview data
    print(f"\nFirst {preview_rows} rows:")
    print(df.head(preview_rows).to_string())

    # Summary statistics for numeric columns
    numeric_cols = df.select_dtypes(include=["int64", "float64"]).columns
    if len(numeric_cols) > 0:
        print("\nNumeric Summary:")
        print(df[numeric_cols].describe().to_string())

    return df


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 load_csv.py <file.csv> [--preview N]")
        sys.exit(1)

    filepath = sys.argv[1]
    preview_rows = 5

    # Parse optional preview argument
    if len(sys.argv) > 2 and sys.argv[2] == "--preview":
        preview_rows = int(sys.argv[3])

    df = load_and_explore(filepath, preview_rows)

    # Return df to interactive session
    print("\nDataFrame loaded as 'df'")
