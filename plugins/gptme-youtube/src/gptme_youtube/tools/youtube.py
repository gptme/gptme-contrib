import logging

from gptme.message import Message
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

    result = llm_summarize(transcript)
    if result is None:
        return "Error: LLM returned no response."
    return result.content


def execute(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
) -> Message:
    """Execute a youtube block by fetching the transcript for the given video ID."""
    video_id = (code or "").strip()
    if not video_id and args:
        video_id = args[0].strip()
    if not video_id:
        return Message("system", "Error: no video ID provided in youtube block.")
    result = get_transcript(video_id)
    return Message("system", result)


tool: ToolSpec = ToolSpec(
    name="youtube",
    desc="Fetch and summarize YouTube video transcripts",
    block_types=["youtube"],
    execute=execute,
    functions=[get_transcript, summarize_transcript],
    available=bool(YouTubeTranscriptApi),
)
