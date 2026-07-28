import unittest
from pathlib import Path


class FollowFeatureVisibilityTest(unittest.TestCase):
    def test_follow_feature_is_hidden_from_the_frontend(self):
        html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="followTab" class="tab hidden"', html)
        self.assertIn("$('followTab').classList.add('hidden')", html)
        self.assertNotIn('data-follow-index="${index}"', html)
        self.assertNotIn('id="detailFollow"', html)
        self.assertNotIn("showApp();loadSiteNotice();loadRequests();loadFollows()", html)
        self.assertIn(
            "请手动选择需要的资源版本，转存前会检查 Emby 入库进度。",
            html,
        )


if __name__ == "__main__":
    unittest.main()
