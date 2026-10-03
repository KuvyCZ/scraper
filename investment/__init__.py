"""investment/ — investiční režim (Šumperk): samostatný modul vedle hlavního monitoringu.

Nic z main.py/notifier.py/database.py se tímto balíčkem nemění — vlastní config,
vlastní DB (investment.db), vlastní Telegram chat, vlastní interval. Spouští se
samostatně přes investment_main.py.
"""
