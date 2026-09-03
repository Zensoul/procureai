from procureai.config import get_settings
s = get_settings()
print("App:", s.app_name)
print("Env:", s.app_env)
print("Is dev:", s.is_development)
print("Log level:", s.log_level)
print("Config OK")