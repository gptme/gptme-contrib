import logging
from urllib.parse import parse_qs, urlparse

from gptme.message import Message
from gptme.tools import ToolSpec
from gptme.tools.base import ConfirmFunc

logger = logging.getLogger(__name__)

try:
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
except ImportError:
    YouTubeTranscriptApi = None


def _extract_video_id(url_or_id: str) -> str:
    """Extract the bare video ID from a YouTube URL or return as-is if already an ID."""
    parsed = urlparse(url_or_id)
    if parsed.scheme in ("http", "https"):
        # Handle https://www.youtube.com/watch?v=VIDEOID
        if "youtube.com" in parsed.netloc:
            qs = parse_qs(parsed.query)
            if "v" in qs:
                return qs["v"][0]
            # Handle https://www.youtube.com/shorts/VIDEOID
            # and https://www.youtube.com/live/VIDEOID
            path_parts = [p for p in parsed.path.split("/") if p]
            for segment in ("shorts", "live"):
                if segment in path_parts:
                    idx = path_parts.index(segment) + 1
                    if idx < len(path_parts):
                        return path_parts[idx]
        # Handle https://youtu.be/VIDEOID
        if "youtu.be" in parsed.netloc:
            return parsed.path.lstrip("/")
    return url_or_id


def get_transcript(video_id: str) -> str:
    """Fetch transcript for a YouTube video by its URL or video ID."""
    if not YouTubeTranscriptApi:
        return "Error: youtube_transcript_api is not installed."
    video_id = _extract_video_id(video_id)
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
    confirm: ConfirmFunc,
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
    instructions="When the user shares a YouTube link or asks about video content, use get_transcript(video_id) to retrieve the full transcript — it accepts watch?v=, youtu.be/, /shorts/, and /live/ URLs as well as bare video IDs. For long transcripts, pass the result to summarize_transcript(transcript) to get a concise summary. You can also emit ```youtube VIDEO_ID blocks to fetch transcripts inline.",
    block_types=["youtube"],
    execute=execute,
    functions=[get_transcript, summarize_transcript],
    available=bool(YouTubeTranscriptApi),
)
