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
# how many tasks a single worker process runs concurrently
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "4"))

TEST_URL = ""
