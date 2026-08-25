import os

##############################
# APP IDENTITY
##############################
ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
PROJECT_NAME = "astra-backend"
REGION = "us-east-1"
VERSION = "0.1.0"

##############################
# QUEUE / WORKER KNOBS
##############################
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "4"))

##############################
# ENGINE COLLECTION KNOBS
##############################
ACTIVE_ENGINES = [engine.strip() for engine in os.environ.get("ACTIVE_ENGINES", "claude,gemini,chatgpt").split(",") if engine.strip()]
# the big cost lever: web searches dominate the collection bill; 0 disables search
MAX_WEB_SEARCHES = int(os.environ.get("MAX_WEB_SEARCHES", "1"))
ENGINE_MAX_CONCURRENCY = int(os.environ.get("ENGINE_MAX_CONCURRENCY", "32"))

# provider API keys, read straight from env (empty = engine unusable at call
# time; adapters construct clients lazily so an engine that's off needs no key)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


TEST_URL = ""

