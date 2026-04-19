import sys

if __name__ == "__main__":
    try:
        from autoreview.cli import main
    except ImportError as e:
        print(
            "autoreview could not be imported (is the package installed?).",
            e,
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
