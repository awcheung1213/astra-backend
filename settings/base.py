import os

##############################
# APP IDENTITY
##############################
ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")  # production overrides the default to INFO
PROJECT_NAME = "evergreen-backend"
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
# #9 technical_fix (agent-tools-plan.md Phase 2): "generators" = the per-kind $0
# code path; "agent" = one Technical-agent run per audit with CMS read tools.
# Flip per environment after comparing proposals on the same findings.
TECHNICAL_FIX_MODE = os.environ.get("TECHNICAL_FIX_MODE", "generators")
# lifetime token budget (input + output) for one agent-mode technical_fix run
TECHNICAL_FIX_TOKEN_BUDGET = int(os.environ.get("TECHNICAL_FIX_TOKEN_BUDGET", "150000"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Google read adapter (GSC + GA4) OAuth app — empty = adapter unusable at call time
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
# where Google's consent redirect lands (must be registered on the OAuth app)
# and where the callback bounces the browser afterwards — local defaults;
# deployed environments override both via env
GOOGLE_OAUTH_REDIRECT_URL = os.environ.get("GOOGLE_OAUTH_REDIRECT_URL", "http://localhost:8000/api/v1/connections/google/callback")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:5173")
# where WordPress's authorize-application screen sends the browser back
# (one-tap connect). WordPress demands https here unless the WP site itself
# is a local install. Defaults to the Google callback's host, so a deployed
# environment needs no new variable.
WORDPRESS_CONNECT_CALLBACK_URL = os.environ.get("WORDPRESS_CONNECT_CALLBACK_URL") or GOOGLE_OAUTH_REDIRECT_URL.replace("/connections/google/callback", "/connections/wordpress/callback")


TEST_URL = ""
