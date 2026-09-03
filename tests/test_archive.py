import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class OfficialGroupArchiveTests(unittest.TestCase):
    def setUp(self):
        app.OFFICIAL_GROUP_ARCHIVE_BY_ID.clear()
        app.OFFICIAL_GROUP_ARCHIVE_BY_TITLE.clear()

    def test_tmdb_title_year_matching_returns_resource_without_link(self):
        resources = [{
            "id": 1,
            "title": "生死奔救 (2026)",
            "media_title": "生死奔救",
            "year": "2026",
            "note": "4K H.265 中文字幕",
            "official_group": "测试官组",
            "share_type": "115",
            "links": ["https://115.com/s/example"],
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official-group-resources.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as file:
                json.dump({"resources": resources}, file, ensure_ascii=False)
            with (
                patch.object(app, "OFFICIAL_GROUP_ARCHIVE_PATH", path),
                patch.object(
                    app,
                    "tmdb_get",
                    return_value={
                        "id": 1,
                        "title": "生死奔救",
                        "original_title": "The Runner",
                        "release_date": "2026-02-01",
                        "alternative_titles": {"results": []},
                        "translations": {"translations": []},
                    },
                ),
            ):
                records = app.official_group_records_for_tmdb("movie", 1)
        self.assertGreaterEqual(len(records), 1)
        resource = app.serialize_official_group_resource(records[0])
        self.assertEqual(resource["provider"], "archive")
        self.assertEqual(resource["share_type_label"], "115")
        self.assertNotIn("links", resource)
        self.assertIn("官组备份", resource["source"])

    def test_hdhive_is_disabled_in_the_shipped_configuration(self):
        self.assertFalse(app.HDHIVE_FEATURE_ENABLED)


if __name__ == "__main__":
    unittest.main()
