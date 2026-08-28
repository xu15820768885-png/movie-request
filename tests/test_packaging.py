import unittest
from pathlib import Path


class ImagePackagingTests(unittest.TestCase):
    def test_docker_image_copies_and_imports_workflow_module(self):
        dockerfile = (
            Path(__file__).parents[1] / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("COPY workflow.py .", dockerfile)
        self.assertIn('RUN python -c "import app, workflow"', dockerfile)


if __name__ == "__main__":
    unittest.main()
