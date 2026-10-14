"""Owned provider subprocess for the shared lesson selector (not a public CLI)."""

import contextlib
import json
import sys
from dataclasses import dataclass
from typing import Any

from .selector import MAX_REQUEST_BYTES, VERSION, validate_request


def select_with_core(request: dict[str, Any]) -> dict[str, Any]:
    """Reuse gptme's provider stack; reject old ambiguous response parsers."""
    from gptme.context.selector.base import ContextItem
    from gptme.context.selector.config import ContextSelectorConfig
    from gptme.context.selector.llm_based import LLMSelector

    selector = LLMSelector(
        ContextSelectorConfig(strategy="llm", llm_model=request["model"])
    )
    # gptme/gptme#4282 changes malformed envelopes from [] to ValueError.
    # Older installs must not silently treat provider errors as explicit none.
    try:
        selector._parse_response("invalid selection envelope")
    except ValueError:
        pass
    else:
        return {
            "version": VERSION,
            "status": "fallback",
            "ids": [],
            "reason": "unsupported_core",
        }

    @dataclass
    class AdvisoryItem(ContextItem):
        item_id: str
        description: str
        attributes: dict[str, Any]

        @property
        def identifier(self) -> str:
            return self.item_id

        @property
        def content(self) -> str:
            return self.description

        @property
        def metadata(self) -> dict[str, Any]:
            return self.attributes

    allowed = {item["id"] for item in request["candidates"]}

    class AdvisorySelector(LLMSelector):
        def _parse_response(self, response: str) -> list[str]:
            ids = super()._parse_response(response)
            if any(identifier not in allowed for identifier in ids):
                raise ValueError("unknown advisory ID")
            return list(dict.fromkeys(ids))

    selector = AdvisorySelector(selector.config)
    selected = selector.select(
        request["query"],
        [
            AdvisoryItem(item["id"], item["description"], item["metadata"])
            for item in request["candidates"]
        ],
        request["max_results"],
    )
    ids = [item.identifier for item in selected]
    return {"version": VERSION, "status": "selected" if ids else "empty", "ids": ids}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        request = validate_request(json.loads(raw))
        # Provider/config diagnostics must never corrupt the stdout protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = select_with_core(request)
    except ImportError:
        result = {
            "version": VERSION,
            "status": "fallback",
            "ids": [],
            "reason": "missing_dependency",
        }
    except ValueError:
        result = {
            "version": VERSION,
            "status": "fallback",
            "ids": [],
            "reason": "invalid_response",
        }
    except Exception:
        result = {
            "version": VERSION,
            "status": "fallback",
            "ids": [],
            "reason": "provider_error",
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
