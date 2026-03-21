# gptme-youtube

YouTube transcript extraction and summarization plugin for [gptme](https://gptme.org).

Moved from gptme core (`tools/youtube.py`) to gptme-contrib as a plugin.

## Features

- **get_transcript**: Fetch transcript for a YouTube video by video ID
- **summarize_transcript**: Summarize a transcript using the LLM

## Installation

```bash
pip install gptme-youtube
```

Or add to your gptme plugin paths in `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-youtube"]
```

## Dependencies

- `youtube_transcript_api>=0.6.1`

## Usage

Once installed, the `youtube` tool is automatically available in gptme sessions:

```
> get_transcript("dQw4w9WgXcQ")
> summarize_transcript(transcript)
```
