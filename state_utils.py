# state_utils.py
import os
import json
import logging
from telegram.ext import Application

log = logging.getLogger("bot")
STATE_FILE = "bot_state.json"

def load_state(app: Application):
    """Загружает состояние из файла в app.bot_data."""
    bot_data = app.bot_data
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                loaded_state = json.load(f)
                bot_data.update(loaded_state)
        except json.JSONDecodeError:
            log.error("Ошибка чтения bot_state.json, использую значения по умолчанию.")

    # Устанавливаем значения по умолчанию, если их нет
    bot_data.setdefault("bot_on", False)
    bot_data.setdefault("monitored_signals", [])
    bot_data.setdefault("deposit", 50)
    bot_data.setdefault("leverage", 100)
    # НОВЫЕ ПЕРЕМЕННЫЕ ДЛЯ ДИАГНОСТИКИ
    bot_data.setdefault("debug_mode_on", False)
    bot_data.setdefault("last_debug_message", "")

    log.info("State loaded into bot_data. Active signals: %d. Deposit: %s, Leverage: %s",
             len(bot_data.get("monitored_signals", [])), bot_data.get('deposit'), bot_data.get('leverage'))

def save_state(app: Application):
    """Сохраняет app.bot_data в файл."""
    try:
        with open(STATE_FILE, "w") as f:
            # Преобразуем set в list для JSON-сериализации, если chat_ids хранятся в bot_data
            data_to_save = app.bot_data.copy()
            if 'chat_ids' in data_to_save and isinstance(data_to_save['chat_ids'], set):
                data_to_save['chat_ids'] = list(data_to_save['chat_ids'])
            json.dump(data_to_save, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")
