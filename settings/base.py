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


TEST_URL = ""

