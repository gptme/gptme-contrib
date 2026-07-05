"""Email complexity detection for routing decisions."""

import re
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class ComplexityScore:
    """Complexity assessment for an email.

    Attributes:
        score: Complexity score from 0.0 to 1.0, where higher values indicate more complex emails
        reasons: List of human-readable reasons contributing to the complexity score
        is_complex: True if score exceeds the complexity threshold
    """

    score: float  # 0.0-1.0, higher = more complex
    reasons: list[str]
    is_complex: bool  # True if score > threshold

    @property
    def summary(self) -> str:
        """Generate human-readable summary of complexity assessment.

        Returns:
            String summary including score and reasons (if any)
        """
        if not self.reasons:
            return f"Simple email (score: {self.score:.2f})"
        return f"Complex email (score: {self.score:.2f}): {', '.join(self.reasons)}"


class ComplexityDetector:
    """Detect email complexity for routing decisions.

    Analyzes emails based on multiple factors including length, questions,
    sensitive keywords, decision requirements, and recipient count.

    Attributes:
        COMPLEXITY_THRESHOLD: Score threshold for marking email as complex (0.6)
        SENSITIVE_KEYWORDS: Set of keywords indicating sensitive/complex topics
        DECISION_PHRASES: Regex patterns for phrases requiring decisions
    """

    # Threshold for marking as complex (0.0-1.0)
    COMPLEXITY_THRESHOLD = 0.6

    # Sensitive keywords that indicate complexity
    SENSITIVE_KEYWORDS = {
        "financial",
        "money",
        "payment",
        "invoice",
        "contract",
        "legal",
        "lawsuit",
        "attorney",
        "court",
        "confidential",
        "private",
        "sensitive",
        "secret",
        "personal",
        "urgent",
        "critical",
        "emergency",
    }

    # Decision-making phrases
    DECISION_PHRASES = [
        r"should\s+we",
        r"which\s+option",
        r"need\s+to\s+decide",
        r"what\s+do\s+you\s+think",
        r"your\s+thoughts",
        r"approve",
        r"sign\s+off",
    ]

    def detect(self, message: EmailMessage, body: str) -> ComplexityScore:
        """Detect complexity of email based on multiple factors.

        Analyzes the email for:
        - Length (word count): weight 0.2
        - Paragraph count: weight 0.15
        - Number of questions: weight 0.25
        - Sensitive keywords: weight 0.3
        - Decision-making phrases: weight 0.25
        - Multiple recipients: weight 0.15

        Args:
            message: Email message object with headers
            body: Plain text body content

        Returns:
            ComplexityScore with overall score, contributing factors, and complexity flag
        """
        reasons = []
        score = 0.0

        # Check length (weight: 0.2)
        word_count = len(body.split())
        if word_count > 500:
            reasons.append(f"long email ({word_count} words)")
            score += 0.2

        # Check paragraphs (weight: 0.15)
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        if len(paragraphs) > 5:
            reasons.append(f"many paragraphs ({len(paragraphs)})")
            score += 0.15

        # Check questions (weight: 0.25)
        questions = body.count("?")
        if questions > 3:
            reasons.append(f"multiple questions ({questions})")
            score += 0.25

        # Check sensitive keywords (weight: 0.3)
        body_lower = body.lower()
        found_sensitive = [kw for kw in self.SENSITIVE_KEYWORDS if kw in body_lower]
        if found_sensitive:
            reasons.append(f"sensitive keywords: {', '.join(found_sensitive)}")
            score += 0.3

        # Check decision phrases (weight: 0.25)
        found_decisions = []
        for pattern in self.DECISION_PHRASES:
            if re.search(pattern, body_lower):
                found_decisions.append(pattern.replace(r"\s+", " "))

        if found_decisions:
            reasons.append("requires decision")
            score += 0.25

        # Check multiple recipients (weight: 0.15)
        to_addrs = message.get_all("to", [])
        cc_addrs = message.get_all("cc", [])
        total_recipients = len(to_addrs) + len(cc_addrs)

        if total_recipients > 2:
            reasons.append(f"multiple recipients ({total_recipients})")
            score += 0.15

        # Cap score at 1.0
        score = min(score, 1.0)

        return ComplexityScore(
            score=score, reasons=reasons, is_complex=score > self.COMPLEXITY_THRESHOLD
        )

    def check_batch(self, messages: list[tuple[EmailMessage, str]]) -> dict[str, ComplexityScore]:
        """Check complexity for multiple emails in batch.

        Args:
            messages: List of (message, body) tuples to analyze

        Returns:
            Dict mapping message_id to ComplexityScore for each email
        """
        results = {}
        for message, body in messages:
            message_id = message.get("message-id", "")
            results[message_id] = self.detect(message, body)

        return results
