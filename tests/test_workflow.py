import unittest

from workflow import (
    episode_numbers_from_json,
    episode_numbers_json,
    message_target_hints,
)


class WorkflowHelperTests(unittest.TestCase):
    def test_episode_numbers_are_normalized_for_stable_idempotency(self):
        self.assertEqual(episode_numbers_json([3, 1, 3, 0]), "[1,3]")
        self.assertEqual(episode_numbers_from_json("[3, 1, 3, 0]"), [1, 3])

    def test_invalid_episode_payload_is_safe(self):
        self.assertEqual(episode_numbers_from_json("not-json"), [])
        self.assertEqual(episode_numbers_from_json('{"episode": 3}'), [])

    def test_nested_message_identifiers_are_extracted_without_title_guessing(self):
        hints = message_target_hints(
            {
                "title": "仙逆更新了",
                "data": {
                    "subscription_id": "55",
                    "media": {"tmdb_id": 223911, "target_key": "tv:5670"},
                },
            }
        )
        self.assertEqual(hints["subscription_ids"], {55})
        self.assertEqual(hints["tmdb_ids"], {223911})
        self.assertEqual(hints["target_keys"], {"tv:5670"})

    def test_message_title_alone_never_creates_a_false_target(self):
        hints = message_target_hints({"title": "仙逆 TMDB 223911 更新"})
        self.assertEqual(hints["subscription_ids"], set())
        self.assertEqual(hints["tmdb_ids"], set())
        self.assertEqual(hints["target_keys"], set())


if __name__ == "__main__":
    unittest.main()
