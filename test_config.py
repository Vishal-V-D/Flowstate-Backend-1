from google.genai import types
from gemini_config import build_live_config
import os

os.environ["GEMINI_API_KEY"] = "dummy"

try:
    config = build_live_config("test history")
    print("Config built successfully")
    print(config)
except Exception as e:
    print(f"Error building config: {e}")
