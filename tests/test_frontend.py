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
            "点击按钮会完整转存你选择的资源，不再自动筛选缺集。",
            html,
        )
        self.assertIn("const scope='manual'", html)
        self.assertIn("const actionLabel='转存此资源'", html)
        self.assertNotIn("安全转存缺失集", html)
        self.assertNotIn("115安全预检", html)
        self.assertNotIn("confirm_whole:", html)


if __name__ == "__main__":
    unittest.main()
