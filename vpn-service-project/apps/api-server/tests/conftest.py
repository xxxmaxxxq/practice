"""Общие фикстуры тестов."""

import os
import sys
from pathlib import Path

# Чтобы тесты запускались и локально, и в CI без установки пакета
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Значения по умолчанию, чтобы Settings не требовал реального .env
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("JWT_SECRET", "test-secret")
