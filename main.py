"""CCFlow demo — run from the project root: python main.py"""

import argparse
import sys

from ccflow import ClaudeOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="CCFlow: Claude Code Orchestrator")
    parser.add_argument("prompt", nargs="?", help="Prompt to send (reads stdin if omitted)")
    parser.add_argument("-m", "--model", default="sonnet", help="Model name (default: sonnet)")
    parser.add_argument("--batch", action="store_true", help="Batch mode (quiet, no streaming)")
    parser.add_argument("--plan", action="store_true", help="Plan mode (read-only exploration)")
    parser.add_argument("--allowed-tools", nargs="*", help="Allowed tools list")
    parser.add_argument("--log-dir", default="logs", help="Log directory (default: logs)")
    parser.add_argument("--cwd", help="Working directory for claude subprocess")
    parser.add_argument("--max-budget", type=float, help="Max budget in USD")
    args = parser.parse_args()

    # Read prompt from arg or stdin
    prompt = args.prompt
    if not prompt:
        if sys.stdin.isatty():
            parser.error("Provide a prompt as argument or pipe via stdin")
        prompt = sys.stdin.read().strip()
    if not prompt:
        parser.error("Empty prompt")

    orc = ClaudeOrchestrator(
        model=args.model,
        allowed_tools=args.allowed_tools,
        permission_mode="plan" if args.plan else None,
        log_dir=args.log_dir,
        cwd=args.cwd,
        max_budget_usd=args.max_budget,
    )

    if args.batch:
        result = orc.run(prompt)
    else:
        result = orc.run_stream(prompt)

    if not result.success:
        print(f"\nError: {result.error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
