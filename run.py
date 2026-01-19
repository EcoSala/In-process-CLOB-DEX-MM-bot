import asyncio
import logging
from src.core.config import load_config
from src.core.logger import setup_logging
from src.core.app import BotApp

log = logging.getLogger("mm")

async def main():
    cfg = load_config("config.yaml")
    setup_logging(cfg.app.log_level)

    app = BotApp(cfg)

    try:
        await app.run()
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt: stopping...")
        app.stop()

if __name__ == "__main__":
    asyncio.run(main())
