import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app
from guanying_client import (
    CaptchaChallenge,
    GuanyingClient,
    allowed_links,
    extract_media_candidates,
    link_kind,
    normalize_base_url,
    normalize_resources,
)


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class GuanyingFoundationTests(unittest.TestCase):
    def test_chinese_primary_domain_is_normalized_to_idna(self):
        self.assertEqual(
            normalize_base_url("https://www.教父.com/"),
            "https://www.xn--wcv59z.com",
        )
        self.assertEqual(
            normalize_base_url("https://www.星际穿越.com"),
            "https://www.xn--kivn76b41nnhi.com",
        )
        with self.assertRaises(ValueError):
            normalize_base_url("https://example.com")

    def test_only_115_magnet_and_ed2k_are_exposed(self):
        links = allowed_links([
            "https://115.com/s/abc", "magnet:?xt=urn:btih:abc",
            "ed2k://|file|S01E01.mkv|100|ABC|/", "https://pan.baidu.com/s/no",
        ])
        self.assertEqual([item["kind"] for item in links], ["115", "magnet", "ed2k"])
        self.assertEqual(link_kind("https://pan.quark.cn/s/no"), "")

    def test_search_page_candidates_and_resource_payload_are_normalized(self):
        html = '<script>window.items={"dir":"tv","id":88,"title":"测试剧","year":"2026"};</script>'
        self.assertEqual(extract_media_candidates(html)[0]["id"], "88")
        resources = normalize_resources(
            {"panlist": [{"title": "测试剧 S01E03", "url": "magnet:?xt=urn:btih:123"}],
             "ignored": {"url": "https://pan.baidu.com/s/no"}},
            media_kind="tv", media_id="88",
        )
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["share_type_label"], "磁力")

    def test_exact_single_episode_filter_rejects_pack_and_wrong_episode(self):
        resources = [
            {"title": "剧名 S01E03", "share_url": "magnet:?xt=urn:btih:one"},
            {"title": "剧名 S01E01-E10", "share_url": "magnet:?xt=urn:btih:pack"},
            {"title": "剧名 S01E04", "share_url": "magnet:?xt=urn:btih:wrong"},
        ]
        exact = app._guanying_exact_resource(resources, 1, 3)
        self.assertEqual([item["share_url"] for item in exact], ["magnet:?xt=urn:btih:one"])
        self.assertTrue(app.parse_episode_spec(exact[0]["title"])["safe_single_episode"])

    def test_adaptive_polling_windows(self):
        with patch.object(app, "db", side_effect=OSError("no db")):
            self.assertEqual(app.guanying_adaptive_interval(3600, True), 1800)
            self.assertEqual(app.guanying_adaptive_interval(48 * 3600, True), 7200)
            self.assertEqual(app.guanying_adaptive_interval(96 * 3600, True), 21600)


class GuanyingDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(app, "DATA_DIR", Path(self.temporary.name))
        self.data_patch.start()
        app.DB_PATH = Path(self.temporary.name) / "test.db"
        with app.CACHE_LOCK:
            app.SETTINGS_CACHE.clear()
        app.init_db()
        self.token = "guanying-test-token"
        with app.db() as connection:
            user = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) VALUES('admin','管理员',?,'admin',?)",
                (app.hash_password("password123"), app.now_iso()),
            )
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                (hashlib.sha256(self.token.encode()).hexdigest(), user.lastrowid,
                 (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
            )
            app.set_setting(connection, "tmdb_token", "test-token")

    def tearDown(self):
        self.data_patch.stop()
        self.temporary.cleanup()

    def test_schema_and_default_magnet_policy(self):
        with app.db() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tv_follows)")}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("guanying_next_check_at", columns)
        self.assertIn("guanying_session", tables)
        self.assertIn("unified_wash_attempts", tables)
        self.assertTrue(app.wash_rule_settings()["magnet_auto_follow"])
        self.assertFalse(app.wash_rule_settings()["magnet_auto_wash"])

    def test_wash_rules_are_single_shared_configuration(self):
        result = asyncio.run(app.update_wash_rules(FakeRequest({
            "resolutions": ["1080p", "2160p"], "platforms": [],
            "audio": ["TrueHD Atmos"], "min_score_gain": 12,
            "magnet_auto_follow": True, "magnet_auto_wash": False,
        }), self.token))
        self.assertEqual(result["resolutions"], ["1080p", "2160p"])
        self.assertEqual(result["audio"], ["TrueHD Atmos"])
        self.assertEqual(result["platforms"], [])
        self.assertEqual(result["min_score_gain"], 12)

    def test_strict_single_magnet_is_submitted_to_115_offline(self):
        with app.db() as connection:
            user_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) VALUES(?,999,'tv','测试剧',?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            follow = connection.execute("SELECT * FROM tv_follows WHERE id=?", (follow_id,)).fetchone()
            app.set_setting(connection, "p115_target_cid", "77")

        class FakeP115:
            def clouddownload_task_add_url(self, payload):
                return {"state": True}

        resource = {
            "resource_key": "gy-magnet-e03",
            "title": "测试剧 S01E03 2160p WEB-DL",
            "share_url": "magnet:?xt=urn:btih:single03",
        }
        with patch.object(app, "p115_client", return_value=FakeP115()):
            self.assertTrue(app._guanying_receive_exact_episode(
                follow, resource, 1, 3, scope="auto_missing"
            ))
        with app.db() as connection:
            job = connection.execute("SELECT state FROM media_workflow_jobs").fetchone()
            transfer = connection.execute("SELECT status,episode_number FROM resource_transfer_log").fetchone()
        self.assertEqual(job["state"], "submitted")
        self.assertEqual((transfer["status"], transfer["episode_number"]), ("submitted", 3))

    def test_calendar_is_private_by_default_and_admin_can_request_all(self):
        with app.db() as connection:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            other = connection.execute(
                "INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES('other','其他人',?,'member',?)",
                (app.hash_password("password123"), app.now_iso()),
            ).lastrowid
            for user_id, title in ((admin_id, "我的剧"), (other, "他人剧")):
                connection.execute(
                    "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) VALUES(?,?,'tv',?,?,?)",
                    (user_id, 100 + user_id, title, app.now_iso(), app.now_iso()),
                )
        detail = {"status": "Returning Series", "seasons": [{"season_number": 1}]}
        season = {"episodes": [{"episode_number": 1, "air_date": "2026-09-07", "name": "第一集"}]}
        def tmdb(path, _params):
            return season if "/season/" in path else detail
        with patch.object(app, "tmdb_get", side_effect=tmdb), patch.object(
            app, "destination_episode_progress", return_value={"emby_episode_numbers": {}}
        ):
            mine = app.follow_calendar("2026-09", "mine", self.token)
            all_items = app.follow_calendar("2026-09", "all", self.token)
        self.assertEqual({item["title"] for item in mine["entries"]}, {"我的剧"})
        self.assertEqual({item["title"] for item in all_items["entries"]}, {"我的剧", "他人剧"})


if __name__ == "__main__":
    unittest.main()
