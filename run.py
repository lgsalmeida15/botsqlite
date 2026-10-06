import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from python.config import load_env  # noqa: E402

load_env()      # o `.env` antes de qualquer modulo que decida o backend (db)

from python.main import main  # noqa: E402

if __name__ == "__main__":
    main()
