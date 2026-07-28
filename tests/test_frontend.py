import unittest
from pathlib import Path


class FollowResourceActionsTest(unittest.TestCase):
    def test_follow_resource_button_performs_hdhive_transfer(self):
        html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-follow-transfer="${follow.id}"', html)
        self.assertIn(">转存此资源</button>", html)
        self.assertIn("transferFollowResource(follow", html)
        self.assertIn("await api('/api/hdhive/transfer'", html)
        self.assertNotIn(">更换为此资源</button>", html)


if __name__ == "__main__":
    unittest.main()
