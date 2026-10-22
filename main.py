"""EduMeet launcher.

    python main.py server          # start the FastAPI backend
    python main.py server --reload # ...with auto-reload (development)
    python main.py desktop         # start the PySide6 desktop client (default)
"""
import argparse
import os
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="EduMeet")
    parser.add_argument("target", nargs="?", choices=["server", "desktop", "test-mail", "ai-check"], default="desktop")
    parser.add_argument("--to", default="", help="address for the test-mail command")
    parser.add_argument("--reload", action="store_true", help="auto-reload the server on code changes")
    args = parser.parse_args()

    if args.target == "test-mail":
        from backend.config import BASE_DIR, settings
        from backend.mailer import send_test, status_line

        env_file = BASE_DIR / ".env"
        print(f"Settings file: {env_file}  ->  {'found' if env_file.exists() else 'NOT FOUND (copy .env.example to .env)'}")
        print(status_line())
        target = args.to or settings.smtp_user
        if not target:
            print("Give an address:  python main.py test-mail --to you@gmail.com")
            return 1
        ok, message = send_test(target)
        print(message)
        return 0 if ok else 1

    if args.target == "ai-check":
        from fastapi import HTTPException

        from backend.config import settings
        from backend.llm import ask_claude, claude_configured
        from backend.transcribe import get_model, whisper_available

        print("--- Claude (summaries, assistant, quiz, notes) ---")
        if not claude_configured():
            print("ANTHROPIC_API_KEY is empty -> these features are OFF. Add the key to .env.")
        else:
            try:
                reply = ask_claude("Reply with the single word OK.", [{"role": "user", "content": "ping"}], 20)
                print(f"Claude works with model {settings.ai_model}: {reply!r}")
            except HTTPException as exc:
                print("Claude problem:", exc.detail)
        print("--- Whisper (live captions) ---")
        ok, why = whisper_available()
        if not ok:
            print(why)
        else:
            print(f"Loading the '{settings.whisper_model}' speech model (the first time it downloads, please wait)...")
            try:
                get_model()
                print("Speech recognition is ready.")
            except Exception as exc:  # noqa: BLE001
                print("Could not load the speech model:", exc)
        return 0

    if args.target == "server":
        import uvicorn

        uvicorn.run(
            "backend.main:app",
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            reload=args.reload,
        )
        return 0

    from desktop.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
