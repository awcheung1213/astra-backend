"""Query normalization for the shared answer corpus: two brands' prompts that
normalize identically share one collected answer. Must stay in lockstep with
the SQL normalization in the query_responses backfill migration."""

import hashlib
import re


def normalize_query(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return re.sub(r"[.?!]+$", "", normalized)


def query_hash(text: str) -> str:
    return hashlib.md5(normalize_query(text).encode()).hexdigest()
