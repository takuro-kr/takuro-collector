from takuro_collector.logging_setup import setup_logging
from takuro_collector.ui import run_app

if __name__ == "__main__":
    setup_logging()
    raise SystemExit(run_app())
