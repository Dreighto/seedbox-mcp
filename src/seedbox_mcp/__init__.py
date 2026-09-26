"""Seedbox MCP."""

import logging

__all__ = ["__version__"]

__version__ = "0.1.0"

# httpx logs every request URL at INFO, and Telegram's Bot API puts the bot token
# in the URL, so every entry point's INFO logging wrote the token to the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
