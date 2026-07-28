import unittest
from pathlib import Path


class FollowResourceActionsTest(unittest.TestCase):
    def test_follow_resource_button_performs_hdhive_transfer(self):
        html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-follow-transfer="${follow.id}"', html)
        self.assertIn("resource.is_pack?'转存整包'", html)
        self.assertIn("`转存第${episodes[0]}集`:'转存此资源'", html)
        self.assertIn("transferFollowResource(follow", html)
        self.assertIn("await api('/api/hdhive/transfer'", html)
        self.assertIn("allow_existing:scope==='whole'&&hasLocal", html)
        self.assertIn("Object.assign(follow,result.follow)", html)
        self.assertIn('data-follow-delete="${item.id}">取消追更</button>', html)
        self.assertIn("已取消本地与影巢追更", html)
        self.assertNotIn("data-native-cancel", html)
        self.assertNotIn("确定取消《${follow.title}》的影巢原生订阅吗", html)
        self.assertNotIn(">更换为此资源</button>", html)


if __name__ == "__main__":
    unittest.main()
