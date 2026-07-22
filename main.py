from dotenv import load_dotenv

from display import show_results
from service import CheckOutcome, check_text, check_url

load_dotenv()


def _print_one_result(result: dict) -> None:
    show_results([result])


def _print_progress(event: dict) -> None:
    if event.get("stage") == "fetching_article" and event.get("state") == "started":
        print("Fetching article with Jina Reader...")


def _report_outcome(outcome: CheckOutcome) -> None:
    if outcome.status in {"completed", "partial", "no_claims", "no_evidence"}:
        return
    prefix = "ERROR: " if outcome.status in {
        "invalid_input", "unreadable", "timeout", "failed"
    } else ""
    print(f"{prefix}{outcome.message}")


def run_article(verbose: bool = False) -> None:
    try:
        url = input("Article URL: ").strip()
        outcome = check_url(
            url,
            on_progress=_print_progress,
            on_result=_print_one_result,
            verbose=verbose,
        )
        _report_outcome(outcome)
    except KeyboardInterrupt:
        print("\nCancelled by user")


def run_text(verbose: bool = False) -> None:
    try:
        print("Paste your text, then press Enter twice when done:")
        lines = []
        while True:
            line = input()
            if not line and lines and not lines[-1]:
                break
            lines.append(line)

        outcome = check_text(
            "\n".join(lines),
            on_result=_print_one_result,
            verbose=verbose,
        )
        _report_outcome(outcome)
    except KeyboardInterrupt:
        print("\nCancelled by user")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Fact Checker")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Log external provider calls and timing",
    )
    args = parser.parse_args()

    print("Fact Checker")
    print("Model: DeepSeek V4 Flash (DeepInfra)\n")
    print("Input mode:")
    print("  1. Article URL")
    print("  2. Paste Text")
    choice = input("\n> ").strip()

    if choice == "1":
        run_article(args.verbose)
    elif choice == "2":
        run_text(args.verbose)
    else:
        print("ERROR: Invalid choice, pick 1 or 2")


if __name__ == "__main__":
    main()
