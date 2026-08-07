import unittest
from pathlib import Path


class RuntimeRequirementsTest(unittest.TestCase):
    def test_telegram_user_session_dependencies_are_installed(self):
        requirements = (
            Path(__file__).parents[1] / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("httpx[http2]", requirements)
        self.assertIn("telethon==1.44.0", requirements)
        self.assertIn("python-socks[asyncio]", requirements)


if __name__ == "__main__":
    unittest.main()
