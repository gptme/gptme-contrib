import logging

from gptme.tools import ToolSpec

logger = logging.getLogger(__name__)

try:
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
except ImportError:
    YouTubeTranscriptApi = None


def get_transcript(video_id: str) -> str:
    """Fetch transcript for a YouTube video by its video ID."""
    if not YouTubeTranscriptApi:
        return "Error: youtube_transcript_api is not installed."
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([entry["text"] for entry in transcript])
    except Exception as e:
        logger.error(f"Error fetching transcript: {e}")
        return f"Error fetching transcript: {e}"


def summarize_transcript(transcript: str) -> str:
    """Summarize a transcript using the LLM."""
    from gptme.llm import summarize as llm_summarize

    return llm_summarize(transcript).content


tool: ToolSpec = ToolSpec(
    name="youtube",
    desc="Fetch and summarize YouTube video transcripts",
    functions=[get_transcript, summarize_transcript],
    block_types=["youtube"],
    available=bool(YouTubeTranscriptApi),
)
