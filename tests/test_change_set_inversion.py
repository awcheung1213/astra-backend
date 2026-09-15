"""invert_operations — pure: a change set's completed ops flipped into the revert payload."""

from objects.change_set import ChangeSet
from worker.tasks.changes import invert_operations


def _change_set(operations: list[dict]) -> ChangeSet:
    return ChangeSet(artifact_id="a", site_id="s", brand_id="b", operations=operations)


def test_a_content_write_reverts_to_the_recorded_content_of_the_same_field():
    # a JSON-LD append is a `content` write the agent composed; the record is the whole previous content
    applied = _change_set([{"id": "op-0", "op": "set_field", "field": "content", "target": "https://x.com/", "value": "<p>old page</p><script>…</script>", "previous_value": "<p>old page</p>", "completed_at": "2026-09-15T00:00:00"}])
    (inverse,) = invert_operations(applied)
    assert (inverse["op"], inverse["field"], inverse["value"]) == ("set_field", "content", "<p>old page</p>")


def test_plain_field_reverts_to_its_previous_value_and_uncompleted_ops_are_skipped():
    applied = _change_set(
        [
            {"id": "op-0", "op": "set_field", "field": "title", "target": "https://x.com/about/", "value": "New", "previous_value": "Old", "completed_at": "2026-09-15T00:00:00"},
            {"id": "op-1", "op": "set_field", "field": "meta_description", "target": "https://x.com/about/", "value": "d", "previous_value": None, "completed_at": None},
        ]
    )
    (inverse,) = invert_operations(applied)
    assert (inverse["field"], inverse["value"]) == ("title", "Old")


def test_publish_without_created_ref_cannot_be_inverted():
    applied = _change_set([{"id": "op-0", "op": "publish_page", "target": None, "value": {}, "previous_value": None, "completed_at": "2026-09-15T00:00:00"}])
    assert invert_operations(applied) is None
