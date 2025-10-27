# Skills CLI Commands

This document describes the CLI commands for the gptme skills system.

## Overview

Skills can be managed via CLI commands similar to the existing `/lesson` commands. These commands should be integrated into gptme core.

## Proposed Commands

### `/skill list`
List all available skills with their descriptions.

**Usage**:
/skill list

**Output**:
Available skills:
  data-analysis    - Analyze datasets using pandas and create visualizations
  web-scraping     - Extract data from websites using BeautifulSoup

### `/skill show <name>`
Show detailed information about a specific skill.

**Usage**:
/skill show data-analysis

**Output**:
Skill: data-analysis
Description: Analyze datasets using pandas and create visualizations
Keywords: data, analysis, pandas, visualization
Tools: analyze.py, visualize.py
Status: active
Version: 1.0.0

[Full SKILL.md content]

### `/skill search <query>`
Search for skills matching keywords or description.

**Usage**: `/skill search data`

**Output**:
Found 2 skills matching 'data':
  data-analysis    - Analyze datasets using pandas
  web-scraping     - Extract data from websites
