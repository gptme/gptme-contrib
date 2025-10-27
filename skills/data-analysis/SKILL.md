---
name: data-analysis
keywords: [data, analysis, pandas, csv, statistics, plot, dataframe, numpy, visualization]
description: General-purpose data analysis using Python, pandas, and visualization tools
tools: [ipython, shell, save, read]
version: "1.0.0"
author: Bob (@TimeToBuildBob)
status: active
examples:
  - Load and explore CSV data
  - Statistical analysis and hypothesis testing
  - Data visualization with matplotlib
  - Data cleaning and transformation
---

# Data Analysis Skill

General-purpose data analysis using Python, pandas, and scientific computing tools available in gptme.

## Overview

This skill provides patterns and tools for common data analysis workflows:
- Loading data from various sources (CSV, JSON, databases)
- Exploratory data analysis (EDA)
- Statistical analysis and hypothesis testing
- Data visualization
- Data cleaning and transformation

## Available Tools

- **pandas**: DataFrames for tabular data
- **numpy**: Numerical computing
- **scipy**: Statistical tests and scientific computing
- **matplotlib**: Plotting and visualization
- **IPython**: Interactive Python execution

## Common Patterns

### 1. Load and Explore CSV Data

```python
import pandas as pd
import numpy as np

# Load CSV file
df = pd.read_csv('data.csv')

# Quick overview
print(df.shape)  # Dimensions
print(df.head())  # First few rows
print(df.info())  # Column types and missing values
print(df.describe())  # Statistical summary

# Check for missing values
print(df.isnull().sum())
```

### 2. Data Cleaning

```python
# Handle missing values
df_clean = df.dropna()  # Remove rows with any missing values
df_clean = df.fillna(0)  # Fill missing values with 0
df_clean = df.fillna(df.mean())  # Fill with column mean

# Remove duplicates
df_clean = df.drop_duplicates()

# Filter rows
df_filtered = df[df['column'] > threshold]

# Type conversions
df['date'] = pd.to_datetime(df['date'])
df['numeric'] = pd.to_numeric(df['numeric'], errors='coerce')
```

### 3. Statistical Analysis

```python
from scipy import stats

# Descriptive statistics
mean = df['column'].mean()
median = df['column'].median()
std = df['column'].std()

# Correlation analysis
correlation = df.corr()
print(correlation)

# T-test (compare two groups)
group1 = df[df['category'] == 'A']['value']
group2 = df[df['category'] == 'B']['value']
t_stat, p_value = stats.ttest_ind(group1, group2)
print(f"t-statistic: {t_stat}, p-value: {p_value}")

# Chi-square test (categorical variables)
contingency_table = pd.crosstab(df['var1'], df['var2'])
chi2, p_value, dof, expected = stats.chi2_contingency(contingency_table)
print(f"Chi-square: {chi2}, p-value: {p_value}")
```

### 4. Data Visualization

```python
import matplotlib.pyplot as plt

# Histogram
plt.figure(figsize=(10, 6))
df['column'].hist(bins=30)
plt.title('Distribution')
plt.xlabel('Value')
plt.ylabel('Frequency')
plt.savefig('histogram.png')
plt.close()

# Scatter plot
plt.figure(figsize=(10, 6))
plt.scatter(df['x'], df['y'])
plt.title('Relationship')
plt.xlabel('X Variable')
plt.ylabel('Y Variable')
plt.savefig('scatter.png')
plt.close()

# Box plot
plt.figure(figsize=(10, 6))
df.boxplot(column='value', by='category')
plt.title('Value by Category')
plt.suptitle('')  # Remove default title
plt.savefig('boxplot.png')
plt.close()

# Time series
df['date'] = pd.to_datetime(df['date'])
df = df.sort_values('date')
plt.figure(figsize=(12, 6))
plt.plot(df['date'], df['value'])
plt.title('Time Series')
plt.xlabel('Date')
plt.ylabel('Value')
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig('timeseries.png')
plt.close()
```

### 5. Grouping and Aggregation

```python
# Group by and aggregate
grouped = df.groupby('category').agg({
    'value': ['mean', 'median', 'std', 'count'],
    'amount': 'sum'
})
print(grouped)

# Pivot tables
pivot = df.pivot_table(
    values='value',
    index='row_var',
    columns='col_var',
    aggfunc='mean'
)
print(pivot)
```

### 6. Save Results

```python
# Save processed data
df_clean.to_csv('data_clean.csv', index=False)

# Save specific columns
df[['col1', 'col2', 'col3']].to_csv('subset.csv', index=False)

# Save as JSON
df.to_json('data.json', orient='records')

# Save summary statistics
summary = df.describe()
summary.to_csv('summary_stats.csv')
```

## Best Practices

1. **Start with EDA**: Always explore data before analysis
   - Check dimensions, types, missing values
   - Look at distributions and outliers
   - Understand relationships between variables

2. **Document assumptions**: Note any assumptions about data
   - Missing value handling strategy
   - Outlier treatment decisions
   - Statistical test assumptions

3. **Visualize before and after**: Plot data before/after transformations
   - Verify cleaning didn't introduce issues
   - Confirm transformations achieved goals

4. **Save intermediate results**: Don't lose work
   - Save cleaned datasets
   - Save analysis results
   - Save figures for reports

5. **Check statistical assumptions**: Verify test requirements
   - Normality for parametric tests
   - Sample size requirements
   - Independence of observations

## Example Workflow

```python
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy import stats

# 1. Load data
df = pd.read_csv('sales_data.csv')

# 2. Explore
print(df.shape)
print(df.head())
print(df.info())
print(df.describe())

# 3. Clean
df_clean = df.dropna()
df_clean['date'] = pd.to_datetime(df_clean['date'])
df_clean = df_clean[df_clean['amount'] > 0]

# 4. Analyze
monthly_sales = df_clean.groupby(df_clean['date'].dt.to_period('M'))['amount'].sum()
avg_sale = df_clean['amount'].mean()
top_products = df_clean.groupby('product')['amount'].sum().sort_values(ascending=False).head(10)

# 5. Visualize
plt.figure(figsize=(12, 6))
monthly_sales.plot()
plt.title('Monthly Sales Trend')
plt.ylabel('Sales Amount')
plt.savefig('monthly_sales.png')
plt.close()

# 6. Save results
df_clean.to_csv('sales_clean.csv', index=False)
monthly_sales.to_csv('monthly_summary.csv')
```

## Related Skills

- **web-scraping**: Gather data from web sources
- **database-query**: Work with SQL databases
- **machine-learning**: Build predictive models

## References

- [pandas documentation](https://pandas.pydata.org/docs/)
- [scipy stats](https://docs.scipy.org/doc/scipy/reference/stats.html)
- [matplotlib gallery](https://matplotlib.org/stable/gallery/)
