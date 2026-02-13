"""
Tool bridge for transforming OpenAI function calls to gptme calls.

Implements the async subagent pattern as suggested by Erik:
- OpenAI function calls spawn async gptme subprocess
- Results collected and returned to conversation
- Audio streaming continues while tools execute
"""

import asyncio
from typing import Optional
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result from a gptme tool execution."""

    success: bool
    output: str
    error: Optional[str] = None


class GptmeToolBridge:
    """
    Bridge between OpenAI function calls and gptme CLI.

    Executes gptme commands asynchronously and returns results.

    Note: This implementation uses subprocess execution with --non-interactive mode.
    This means:
    - Tools that require user input will fail or hang
    - Interactive prompts are not supported
    - Long-running operations are limited by the timeout parameter

    For production use, consider implementing a tool allowlist to filter
    supported tools, similar to packages/gptme-runloops/src/gptme_runloops/utils/execution.py
    """

    def __init__(
        self,
        gptme_path: str = "gptme",
        timeout: int = 60,
        workspace: Optional[str] = None,
    ):
        """
        Initialize the tool bridge.

        Args:
            gptme_path: Path to gptme executable
            timeout: Maximum time to wait for gptme response (seconds)
            workspace: Working directory for gptme commands
        """
        self.gptme_path = gptme_path
        self.timeout = timeout
        self.workspace = workspace

    async def execute(self, command: str) -> ToolResult:
        """
        Execute a gptme command asynchronously.

        Args:
            command: The gptme command/prompt to execute

        Returns:
            ToolResult with success status and output
        """
        try:
            # Run gptme in non-interactive mode
            process = await asyncio.create_subprocess_exec(
                self.gptme_path,
                "--non-interactive",
                "--quiet",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )

            # Wait with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Command timed out after {self.timeout} seconds",
                )

            # Decode output
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode != 0:
                return ToolResult(
                    success=False,
                    output=output,
                    error=error or f"Process exited with code {process.returncode}",
                )

            return ToolResult(success=True, output=output, error=None)

        except FileNotFoundError:
            return ToolResult(
                success=False, output="", error=f"gptme not found at {self.gptme_path}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    async def handle_function_call(self, name: str, arguments: dict) -> dict:
        """
        Handle an OpenAI function call.

        Args:
            name: Function name (e.g., "gptme_tool")
            arguments: Function arguments

        Returns:
            Result dict to send back to OpenAI
        """
        if name == "gptme_tool":
            command = arguments.get("command", "")
            if not command:
                return {"error": "No command provided"}

            result = await self.execute(command)

            if result.success:
                return {"result": result.output}
            else:
                return {"error": result.error or "Unknown error"}

        return {"error": f"Unknown function: {name}"}


# Example usage for testing
async def main():
    """Test the tool bridge."""
    bridge = GptmeToolBridge()

    # Test a simple command
    result = await bridge.execute("What is 2 + 2?")
    print(f"Success: {result.success}")
    print(f"Output: {result.output}")
    if result.error:
        print(f"Error: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
