import unittest

import app


class ResourceSourceRetirementTests(unittest.TestCase):
    def test_only_dian_resource_routes_are_exposed(self):
        paths = {route.path for route in app.APP.routes}
        self.assertIn("/api/dian/resources/{media_type}/{tmdb_id}", paths)
        self.assertIn("/api/dian/transfer", paths)
        self.assertNotIn("/api/archive/resources/{media_type}/{tmdb_id}", paths)
        self.assertNotIn("/api/archive/transfer", paths)

    def test_hdhive_backend_is_configured_for_admin_trial(self):
        self.assertFalse(app.HDHIVE_BACKGROUND_ENABLED)
        self.assertEqual(app.HDHIVE_BASE_URL, "https://re0.me")
        self.assertIn("/api/hdhive/resources/{media_type}/{tmdb_id}", {
            route.path for route in app.APP.routes
        })
        self.assertTrue(callable(app.hdhive_resources))


if __name__ == "__main__":
    unittest.main()
