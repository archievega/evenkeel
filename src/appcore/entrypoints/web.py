import sys

import uvicorn

from appcore.setup.config import load_settings, production_config_problems


def main() -> int:
    settings = load_settings()

    problems = production_config_problems(settings)
    if problems and not settings.app.debug:
        # Fail fast rather than serve. A process that boots with a default
        # secret is indistinguishable from a correctly configured one until
        # someone exploits it, and the log line warning about it will have
        # scrolled away hours earlier.
        for problem in problems:
            print(f"FATAL: insecure configuration: {problem}", file=sys.stderr)
        return 1

    workers = max(1, settings.app.workers)
    uvicorn.run(
        "appcore.setup.app_factory:create_app",
        factory=True,
        host=settings.app.host,
        port=settings.app.port,
        workers=workers,
        # reload forks its own supervisor and is incompatible with workers > 1.
        reload=settings.app.reload and workers == 1,
        timeout_keep_alive=settings.app.timeout_keep_alive,
        timeout_graceful_shutdown=settings.app.timeout_graceful_shutdown,
        log_level=settings.app.logging.level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
