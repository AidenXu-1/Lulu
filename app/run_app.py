"""Run the Lulu product GUI from this source checkout."""
import multiprocessing


def main() -> int:
    from src.main import main as gui_main
    return gui_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
