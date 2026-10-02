# gptme-youtube

Let [gptme](https://gptme.org) read YouTube videos: fetch a video's transcript from a URL or video ID, and summarize it with the cheaper summary model of your configured provider.

**Status:** experimental. This was previously a built-in gptme tool (`tools/youtube.py`) and now lives here as a plugin.

## Install

Install into the same Python environment as gptme, including the `youtube` extra (which pulls in [`youtube-transcript-api`](https://pypi.org/project/youtube-transcript-api/)). The package registers itself through the `gptme.plugins` entry point:

```sh
pip install "gptme-youtube[youtube] @ git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-youtube"
# or, for a pipx-installed gptme:
pipx inject gptme "gptme-youtube[youtube] @ git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-youtube"
```

Alternatively, add the plugin directory to `gptme.toml` and install `youtube-transcript-api` into gptme's environment yourself:

```toml
[plugins]
paths = ["/path/to/gptme-contrib/plugins/gptme-youtube"]
enabled = ["gptme_youtube"]
```

The `youtube` tool only becomes available when `youtube-transcript-api` can be imported.

## Quickstart

```sh
gptme "summarize https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

The agent can call the tool's functions from a Python block:

```python
transcript = get_transcript("https://youtu.be/dQw4w9WgXcQ")
print(summarize_transcript(transcript))
```

or fetch a transcript with a `youtube` block containing a URL or video ID:

````
```youtube
dQw4w9WgXcQ
```
````

## Functions

- `get_transcript(video_id)`: returns the transcript as a single string. Accepts a bare video ID or a `youtube.com/watch?v=`, `youtu.be/`, `/shorts/` or `/live/` URL. Errors are returned as a string starting with `Error`.
- `summarize_transcript(transcript)`: summarizes text with gptme's built-in summarizer. For long transcripts, that summarizer currently only looks at roughly the first and last 400 words, so for a full-video summary it's often better to let the agent read the transcript directly.

Transcripts come from YouTube's caption data, so videos without captions (manual or auto-generated) can't be fetched.

## Development

```sh
# from the gptme-contrib repo root
uv run pytest plugins/gptme-youtube/tests
```
