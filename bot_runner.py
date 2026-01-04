# bot_runner.py - для запуска только бота в webhook режиме
import asyncio
import os
from app import run_bot

if __name__ == "__main__":
    asyncio.run(run_bot())