"""Configuration validation utilities."""

from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple


class ConfigValidator:
    """
    Configuration validator with common validation rules.

    Provides reusable validation functions for configuration values.
    """

    @staticmethod
    def validate_email(email: str) -> Tuple[bool, str]:
        """
        Validate email address format.

        Args:
            email: Email address to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not email:
            return False, "Email address cannot be empty"

        if "@" not in email:
            return False, f"Invalid email format: {email} (missing @)"

        parts = email.split("@")
        if len(parts) != 2:
            return False, f"Invalid email format: {email} (multiple @)"

        local, domain = parts
        if not local or not domain:
            return False, f"Invalid email format: {email} (empty local or domain)"

        if "." not in domain:
            return False, f"Invalid email format: {email} (invalid domain)"

        return True, ""

    @staticmethod
    def validate_path_exists(path: Path, path_type: str = "path") -> Tuple[bool, str]:
        """
        Validate that a path exists.

        Args:
            path: Path to validate
            path_type: Description of what the path represents

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not path.exists():
            return False, f"{path_type} not found: {path}"

        return True, ""

    @staticmethod
    def validate_token(
        token: str | None, token_name: str = "token", min_length: int = 20
    ) -> Tuple[bool, str]:
        """
        Validate API token format.

        Args:
            token: Token to validate
            token_name: Name of the token (for error messages)
            min_length: Minimum expected token length

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not token:
            return False, f"{token_name} not set"

        if len(token) < min_length:
            return (
                False,
                f"{token_name} too short (expected at least {min_length} chars)",
            )

        return True, ""

    @staticmethod
    def validate_positive_number(
        value: float | int, name: str = "value"
    ) -> Tuple[bool, str]:
        """
        Validate that a number is positive.

        Args:
            value: Number to validate
            name: Name of the value (for error messages)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if value <= 0:
            return False, f"{name} must be positive (got: {value})"

        return True, ""

    @staticmethod
    def validate_required_fields(
        config: Dict[str, Any], required_fields: List[str]
    ) -> Tuple[bool, str]:
        """
        Validate that required fields are present and non-empty.

        Args:
            config: Configuration dictionary
            required_fields: List of required field names

        Returns:
            Tuple of (is_valid, error_message)
        """
        missing = []
        for field in required_fields:
            if field not in config or not config[field]:
                missing.append(field)

        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"

        return True, ""

    @staticmethod
    def validate_choice(
        value: Any, choices: List[Any], name: str = "value"
    ) -> Tuple[bool, str]:
        """
        Validate that a value is in a list of allowed choices.

        Args:
            value: Value to validate
            choices: List of allowed values
            name: Name of the value (for error messages)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if value not in choices:
            return (
                False,
                f"{name} must be one of {choices} (got: {value})",
            )

        return True, ""

    @staticmethod
    def validate_url(url: str, name: str = "URL") -> Tuple[bool, str]:
        """
        Validate URL format (basic check).

        Args:
            url: URL to validate
            name: Name of the URL (for error messages)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not url:
            return False, f"{name} cannot be empty"

        if not url.startswith(("http://", "https://")):
            return False, f"{name} must start with http:// or https://: {url}"

        return True, ""

    @staticmethod
    def combine_validations(
        validations: List[Tuple[bool, str]],
    ) -> Tuple[bool, List[str]]:
        """
        Combine multiple validation results.

        Args:
            validations: List of (is_valid, error_message) tuples

        Returns:
            Tuple of (all_valid, list_of_errors)
        """
        errors = [error for is_valid, error in validations if not is_valid]
        return len(errors) == 0, errors

    @staticmethod
    def validate_with_custom(
        value: Any, validator: Callable[[Any], Tuple[bool, str]]
    ) -> Tuple[bool, str]:
        """
        Apply custom validation function.

        Args:
            value: Value to validate
            validator: Custom validation function

        Returns:
            Result from validator function
        """
        return validator(value)
