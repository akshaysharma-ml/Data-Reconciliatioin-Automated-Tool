"""Convenience entry point: `python run.py` from the project root."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / 'src'
sys.path.insert(0, str(SRC))

import uvicorn  # noqa: E402

from utils.config import get_config  # noqa: E402

if __name__ == '__main__':
    config = get_config()
    uvicorn.run(
        'main:app',
        host=config.get('server.host', '0.0.0.0'),
        port=int(config.get('server.port', 8000)),
        reload=bool(config.get('server.debug', False)),
        app_dir=str(SRC),
    )
