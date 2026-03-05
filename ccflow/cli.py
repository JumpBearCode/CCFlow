"""CCFlow CLI entry point — installed as `ccflow` command."""

import argparse
import sys

from ccflow import ClaudeOrchestrator


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "bot":
        from ccflow.telegram_bot import bot_main
        bot_main(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(description="CCFlow: Claude Code Orchestrator")
    parser.add_argument("prompt", nargs="?", help="Prompt to send (reads stdin if omitted)")
    parser.add_argument("-m", "--model", default="opus", help="Model name (default: opus)")
    parser.add_argument("--batch", action="store_true", help="Batch mode (quiet, no streaming)")
    parser.add_argument("--plan", action="store_true", help="Plan mode (read-only exploration)")
    parser.add_argument("--danger", action="store_true", help="Dangerously skip all permission checks")
    parser.add_argument("-r", "--resume", metavar="SESSION_ID", help="Resume a previous session by ID")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true", help="Continue the most recent session")
    parser.add_argument("-i", "--chat", action="store_true", help="Interactive multi-round conversation")
    parser.add_argument("--allowed-tools", nargs="*", help="Allowed tools list")
    parser.add_argument("--log-dir", default="logs", help="Log directory (default: logs)")
    parser.add_argument("--output-dir", help="Save result output to this directory as .md files")
    parser.add_argument("--cwd", help="Working directory for claude subprocess")
    parser.add_argument("--max-budget", type=float, help="Max budget in USD")
    args = parser.parse_args()

    # Read prompt from arg or stdin
    prompt = args.prompt
    if not prompt:
        if sys.stdin.isatty():
            if not args.resume and not args.continue_session and not args.chat:
                parser.error("Provide a prompt as argument or pipe via stdin")
            prompt = ""  # resume/continue/chat can work with empty prompt
        else:
            prompt = sys.stdin.read().strip()
    if not prompt and not args.resume and not args.continue_session and not args.chat:
        parser.error("Empty prompt")

    orc = ClaudeOrchestrator(
        model=args.model,
        allowed_tools=args.allowed_tools,
        permission_mode="plan" if args.plan else None,
        dangerously_skip_permissions=args.danger,
        resume_session=args.resume,
        continue_session="true" if args.continue_session else None,
        log_dir=args.log_dir,
        output_dir=args.output_dir,
        cwd=args.cwd,
        max_budget_usd=args.max_budget,
    )

    if args.chat:
        results = orc.run_conversation(prompt if prompt else None)
        if not results:
            sys.exit(0)
        last = results[-1]
        if not last.success:
            print(f"\nError: {last.error}", file=sys.stderr)
            sys.exit(1)
    elif args.batch:
        result = orc.run(prompt)
        if not result.success:
            print(f"\nError: {result.error}", file=sys.stderr)
            sys.exit(1)
    else:
        result = orc.run_stream(prompt)
        if not result.success:
            print(f"\nError: {result.error}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
