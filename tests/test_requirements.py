import unittest
from pathlib import Path


class RuntimeRequirementsTest(unittest.TestCase):
    def test_httpx_http2_extra_is_installed_for_p123_password_login(self):
        requirements = (
            Path(__file__).parents[1] / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("httpx[http2]", requirements)


if __name__ == "__main__":
    unittest.main()
