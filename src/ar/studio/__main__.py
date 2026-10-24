"""`python -m ar.studio [--port N] [--workdir PATH] [--provider P] [--model M] [--fake]`"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from ar.studio.server import serve

REPO_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ar studio", description="Open Res Loop, the research studio, in your browser.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", type=Path, help="session storage directory (default: ~/.ar-studio/projects)")
    parser.add_argument("--provider", choices=["openai", "anthropic"], help="default: whichever research API key is in .env")
    parser.add_argument("--model", help="Model id for the configured research provider")
    parser.add_argument("--fake", action="store_true", help="demo mode on the built-in papers, no API key")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    serve_kwargs = {"workdir": args.workdir} if args.workdir is not None else {}
    httpd = serve(args.port, provider=args.provider, model=args.model, fake=args.fake, **serve_kwargs,
                  env_files=(Path.cwd() / ".env", REPO_ROOT / ".env"))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Res Loop: {url}  ({httpd.studio.mode})  Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
