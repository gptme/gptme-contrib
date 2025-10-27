#!/usr/bin/env python3
"""
Statistical summary helper for quick analysis.

Usage:
    python3 stats_summary.py data.csv
    python3 stats_summary.py data.csv --column specific_column
    python3 stats_summary.py data.csv --group category_column
"""

import sys
import pandas as pd  # type: ignore
from scipy import stats  # type: ignore


def full_summary(df):
    """Generate comprehensive statistical summary."""
    print("=" * 60)
    print("STATISTICAL SUMMARY")
    print("=" * 60)

    # Basic info
    print(f"\nDataset: {df.shape[0]} rows × {df.shape[1]} columns")

    # Numeric columns
    numeric_cols = df.select_dtypes(include=["int64", "float64"]).columns
    if len(numeric_cols) > 0:
        print("\n" + "-" * 60)
        print("NUMERIC COLUMNS")
        print("-" * 60)
        print(df[numeric_cols].describe().to_string())

        # Correlation matrix
        if len(numeric_cols) > 1:
            print("\n" + "-" * 60)
            print("CORRELATION MATRIX")
            print("-" * 60)
            corr = df[numeric_cols].corr()
            print(corr.to_string())

    # Categorical columns
    categorical_cols = df.select_dtypes(include=["object", "category"]).columns
    if len(categorical_cols) > 0:
        print("\n" + "-" * 60)
        print("CATEGORICAL COLUMNS")
        print("-" * 60)
        for col in categorical_cols:
            print(f"\n{col}:")
            value_counts = df[col].value_counts()
            print(value_counts.head(10).to_string())
            if len(value_counts) > 10:
                print(f"... and {len(value_counts) - 10} more unique values")

    # Missing values
    missing = df.isnull().sum()
    if missing.sum() > 0:
        print("\n" + "-" * 60)
        print("MISSING VALUES")
        print("-" * 60)
        missing_pct = (missing / len(df)) * 100
        missing_df = pd.DataFrame({"Count": missing[missing > 0], "Percentage": missing_pct[missing > 0]})
        print(missing_df.to_string())


def column_summary(df, column):
    """Detailed summary for specific column."""
    print("=" * 60)
    print(f"COLUMN SUMMARY: {column}")
    print("=" * 60)

    col_data = df[column]

    # Check if numeric
    if pd.api.types.is_numeric_dtype(col_data):
        print("\nDescriptive Statistics:")
        print(f"  Count:    {col_data.count()}")
        print(f"  Mean:     {col_data.mean():.4f}")
        print(f"  Median:   {col_data.median():.4f}")
        print(f"  Std Dev:  {col_data.std():.4f}")
        print(f"  Min:      {col_data.min():.4f}")
        print(f"  Max:      {col_data.max():.4f}")
        print(f"  Range:    {col_data.max() - col_data.min():.4f}")

        # Quartiles
        print("\nQuartiles:")
        print(f"  Q1 (25%): {col_data.quantile(0.25):.4f}")
        print(f"  Q2 (50%): {col_data.quantile(0.50):.4f}")
        print(f"  Q3 (75%): {col_data.quantile(0.75):.4f}")
        print(f"  IQR:      {col_data.quantile(0.75) - col_data.quantile(0.25):.4f}")

        # Distribution tests
        print("\nDistribution Tests:")
        # Remove NaN for tests
        clean_data = col_data.dropna()
        if len(clean_data) > 0:
            # Normality test (Shapiro-Wilk)
            if len(clean_data) <= 5000:  # Shapiro-Wilk works best on smaller samples
                stat, p_value = stats.shapiro(clean_data)
                print("  Shapiro-Wilk normality test:")
                print(f"    statistic: {stat:.4f}")
                print(f"    p-value:   {p_value:.4f}")
                if p_value < 0.05:
                    print("    → Data likely NOT normally distributed (p < 0.05)")
                else:
                    print("    → Data may be normally distributed (p >= 0.05)")

        # Missing values
        missing = col_data.isnull().sum()
        missing_pct = (missing / len(col_data)) * 100
        print(f"\nMissing Values: {missing} ({missing_pct:.1f}%)")

    else:
        # Categorical column
        print("\nValue Counts:")
        value_counts = col_data.value_counts()
        print(value_counts.head(20).to_string())
        if len(value_counts) > 20:
            print(f"\n... and {len(value_counts) - 20} more unique values")

        print(f"\nUnique Values: {col_data.nunique()}")
        print(f"Missing Values: {col_data.isnull().sum()} ({col_data.isnull().sum() / len(col_data) * 100:.1f}%)")


def grouped_summary(df, group_column):
    """Summary statistics grouped by a categorical column."""
    print("=" * 60)
    print(f"GROUPED SUMMARY by {group_column}")
    print("=" * 60)

    numeric_cols = df.select_dtypes(include=["int64", "float64"]).columns

    if len(numeric_cols) == 0:
        print("No numeric columns to summarize")
        return

    # Group and aggregate
    grouped = df.groupby(group_column)[numeric_cols].agg(["count", "mean", "median", "std", "min", "max"])
    print(grouped.to_string())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Full summary:        python3 stats_summary.py data.csv")
        print("  Column summary:      python3 stats_summary.py data.csv --column name")
        print("  Grouped summary:     python3 stats_summary.py data.csv --group category")
        sys.exit(1)

    filepath = sys.argv[1]
    df = pd.read_csv(filepath)

    if "--column" in sys.argv:
        col_idx = sys.argv.index("--column")
        column = sys.argv[col_idx + 1]
        column_summary(df, column)

    elif "--group" in sys.argv:
        group_idx = sys.argv.index("--group")
        group_column = sys.argv[group_idx + 1]
        grouped_summary(df, group_column)

    else:
        full_summary(df)
