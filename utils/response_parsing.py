"""Deterministic parsing of ranked mentions in AI responses.

Most AI search answers rank options in numbered or bulleted lists. Reading the
position out of the text in Python is free and exact; the LLM extraction pass
is only needed where parsing finds nothing (prose-style answers)."""

import re

NUMBERED_ITEM_REGEX = re.compile(r"^\s{0,3}(\d+)[.)]\s+", re.MULTILINE)
BULLET_ITEM_REGEX = re.compile(r"^\s{0,3}[-*•]\s+", re.MULTILINE)


def split_list_items(text: str) -> list[str]:
    for pattern in (NUMBERED_ITEM_REGEX, BULLET_ITEM_REGEX):
        list_items_start_indices = [list_item.start() for list_item in pattern.finditer(text)]
        if len(list_items_start_indices) >= 2:  # a single "1." is usually not a list
            list_items = []
            for i, start_index in enumerate(list_items_start_indices):
                end_index = list_items_start_indices[i + 1] if i + 1 < len(list_items_start_indices) else len(text)
                list_item = text[start_index:end_index]
                # the last item ends at the first paragraph break
                if i + 1 == len(list_items_start_indices):
                    list_item = list_item.split("\n\n", 1)[0]
                list_items.append(list_item)
            return list_items
    return []


def parse_prompt_result_for_mention_rank(text: str, names: list[str]) -> dict[str, int | None]:
    list_items = split_list_items(text)
    mention_ranks: dict[str, int | None] = {}
    for name in names:
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        mention_ranks[name] = None
        for index, item in enumerate(list_items, start=1):
            if pattern.search(item):
                mention_ranks[name] = index
                break
    return mention_ranks
