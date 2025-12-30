---
match:
  keywords:
    - compression
    - llmlingua
    - token pruning
    - prompt compression
    - text compression
    - llm compression
---
# LLMLingua (Compression Technique)

## Term
**LLMLingua**: LLM-based prompt compression technique

## Definition
Intelligent text compression method that uses language models to identify and preserve the most important tokens/sentences while removing redundant information

## Method
- **Token-level pruning**: Removes less important tokens based on attention scores
- **Sentence-level extraction**: Selects most relevant sentences using embeddings
- **Information preservation**: Maintains semantic meaning while reducing length

## Performance
- Achieves up to 10x compression ratios
- Maintains task performance with significant token reduction
- Works across various LLM architectures and tasks

## Variants
- **LLMLingua-1**: Original prompt compression approach
- **LLMLingua-2**: Improved with better token selection algorithms
- **Selective Context**: Related technique for context-aware compression

## Purpose
Reduces API costs and increases effective context window size while preserving task completion quality

## Context
Key technique referenced in context compression research for practical token reduction without quality loss.

## Related
- Context compression
- Token optimization
- Prompt engineering
- Cost optimization