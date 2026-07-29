import unittest
from pathlib import Path


class FollowFeatureVisibilityTest(unittest.TestCase):
    def test_native_follow_is_available_without_automatic_transfer(self):
        html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="followTab" class="tab hidden"', html)
        self.assertIn("$('followTab').classList.remove('hidden')", html)
        self.assertIn("我的追更", html)
        self.assertIn("查看自己开启的影巢追更", html)
        self.assertNotIn('data-follow-index="${index}"', html)
        self.assertIn('id="detailFollow"', html)
        self.assertIn("item.series_status==='ongoing'", html)
        self.assertIn("slug:resource.slug", html)
        self.assertIn('class="poster-library-status ${item.in_library?', html)
        self.assertIn("item.in_library?'已入库':'未入库'", html)
        self.assertNotIn("showApp();loadSiteNotice();loadRequests();loadFollows()", html)
        self.assertIn(
            "求片网站只转存你手动选择的资源。",
            html,
        )
        self.assertIn("影巢机器人推送更新链接", html)
        self.assertIn("const scope='manual'", html)
        self.assertIn("const actionLabel='转存此资源'", html)
        self.assertIn('id="detailRequest"', html)
        self.assertIn("提交求片需求", html)
        self.assertIn("${requestShortcut}</div>", html)
        self.assertNotIn("安全转存缺失集", html)
        self.assertNotIn("115安全预检", html)
        self.assertNotIn("confirm_whole:", html)
        self.assertNotIn("按实际缺集自动安全补齐", html)


if __name__ == "__main__":
    unittest.main()
