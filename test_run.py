from procureai.config import get_settings
s = get_settings()
print("DB URL:", s.database_url[:50])
