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

    def test_current_compact_search_and_download_payload_are_normalized(self):
        document = (
            '<script>_obj.search={"l":{"title":["给阿嬷的情书"],'
            '"year":[2026],"d":["mv"],"i":["eGe5d"]}};</script>'
        )
        self.assertEqual(extract_media_candidates(document), [{
            "kind": "mv", "id": "eGe5d", "title": "给阿嬷的情书", "year": "2026",
        }])
        resources = normalize_resources({
            "downlist": {"list": {
                "m": ["0123456789abcdef0123456789abcdef01234567"],
                "t": ["给阿嬷的情书 2026 1080p"],
                "s": ["4.2 GB"],
            }},
            "panlist": {
                "name": ["115资源", "夸克资源"],
                "url": ["https://115.com/s/abc", "https://pan.quark.cn/s/no"],
            },
        }, media_kind="mv", media_id="eGe5d")
        self.assertEqual(len(resources), 2)
        self.assertEqual(
            {item["link_type"] for item in resources}, {"115", "magnet"}
        )
        magnet = next(item for item in resources if item["link_type"] == "magnet")
        self.assertEqual(magnet["title"], "给阿嬷的情书 2026 1080p")
        self.assertTrue(magnet["share_url"].startswith("magnet:?xt=urn:btih:"))

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
        self.assertIn("wash_window_days", columns)
        self.assertTrue(app.wash_rule_settings()["magnet_auto_follow"])
        self.assertFalse(app.wash_rule_settings()["magnet_auto_wash"])

    def test_follow_sources_have_independent_background_switches(self):
        asyncio.run(app.guanying_config(FakeRequest({
            "base_url": "https://www.教父.com", "follow_enabled": False,
        }), self.token))
        asyncio.run(app.update_settings(FakeRequest({
            "dian_follow_enabled": True, "dian_follow_interval": 10800,
        }), self.token))
        self.assertFalse(app.guanying_public_status()["follow_enabled"])
        settings = app.get_settings(self.token)
        self.assertTrue(settings["dian_follow_enabled"])
        self.assertEqual(settings["dian_follow_interval"], 10800)
        with app.db() as connection:
            self.assertNotEqual(app.setting(connection, "guanying_enabled"), "0")

    def test_default_and_per_follow_wash_windows(self):
        asyncio.run(app.update_wash_rules(FakeRequest({
            "default_wash_days": 4,
        }), self.token))
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) "
                "VALUES(?,999,'tv','测试剧',?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            follow = connection.execute(
                "SELECT * FROM tv_follows WHERE id=?", (follow_id,)
            ).fetchone()
        self.assertEqual(app.effective_follow_wash_days(follow), 4)
        result = asyncio.run(app.update_follow_wash_window(
            int(follow_id), FakeRequest({"days": 6}), self.token
        ))
        self.assertEqual(result["follow"]["wash_window_days"], 6)
        self.assertEqual(result["follow"]["wash_window_effective_days"], 6)

    def test_wash_window_is_limited_to_one_through_seven_days(self):
        with self.assertRaises(app.HTTPException):
            asyncio.run(app.update_wash_rules(FakeRequest({
                "default_wash_days": 8,
            }), self.token))
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) "
                "VALUES(?,998,'tv','七天限制测试',?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
        with self.assertRaises(app.HTTPException):
            asyncio.run(app.update_follow_wash_window(
                int(follow_id), FakeRequest({"days": 8}), self.token
            ))

    def test_old_long_or_unlimited_wash_windows_migrate_to_seven_days(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,wash_window_days,created_at,updated_at) "
                "VALUES(?,997,'tv','旧期限测试',-1,?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            app.set_setting(connection, "wash_default_days", "90")
            app.set_setting(connection, "wash_window_max_seven_days_v1", "0")
        app.init_db()
        with app.db() as connection:
            follow = connection.execute(
                "SELECT wash_window_days FROM tv_follows WHERE id = ?", (follow_id,)
            ).fetchone()
            self.assertEqual(app.setting(connection, "wash_default_days"), "7")
        self.assertEqual(int(follow["wash_window_days"]), 7)

    def test_dian_cycle_only_scans_active_tv_follows_when_enabled(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) "
                "VALUES(?,1001,'tv','追更剧',?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            )
            connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,active,created_at,updated_at) "
                "VALUES(?,1002,'tv','已停用剧',0,?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            )
            app.set_setting(connection, "dian_base_url", "https://dian.example")
            app.set_setting(connection, "dian_api_key", "test-key")
            app.set_setting(connection, "dian_follow_enabled", "1")
            app.set_setting(connection, "dian_follow_interval", "3600")
        with patch.object(
            app, "auto_replenish_dian_follow",
            return_value={"checked": True, "transferred": [[1, 8]], "washed": []},
        ) as replenish:
            result = app.dian_follow_once(force=True)
        self.assertEqual(result, {
            "checked": 1, "transferred": 1, "washed": 0, "failed": 0,
        })
        replenish.assert_called_once()

    def test_dian_uses_exact_single_resources_for_follow_and_wash(self):
        resources = [
            {"title": "测试剧 S01E08 2160p", "season_number": 1,
             "episode_numbers": [8], "safe_single_episode": True},
            {"title": "测试剧 S01E01-E10", "season_number": 1,
             "episode_numbers": list(range(1, 11)), "safe_single_episode": False},
        ]
        exact = app._dian_exact_resources(resources, 1, 8)
        self.assertEqual([item["title"] for item in exact], ["测试剧 S01E08 2160p"])

    def test_dian_follow_replenishes_missing_and_washes_latest_episode(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id,tmdb_id,media_type,title,created_at,updated_at) "
                "VALUES(?,301418,'tv','现在不是出轨的问题',?,?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
        aired = [
            {"season_number": 1, "episode_number": 7, "air_date": app.now_iso()[:10]},
            {"season_number": 1, "episode_number": 8, "air_date": app.now_iso()[:10]},
        ]
        progress = {
            "emby_latest_season_number": 1,
            "emby_latest_episode_number": 7,
            "emby_episode_numbers": {"1": list(range(1, 8))},
            "emby_episode_files": {"1": {"7": {
                "file_name": "Show.S01E07.1080p.WEBRip.H264.AAC.mkv",
                "file_size": 2 * 1024**3,
            }}},
        }
        resources = [
            {"share_id": 1, "resource_id": 8, "title": "Show.S01E08.1080p.WEB-DL.mkv",
             "season_number": 1, "episode_numbers": [8], "safe_single_episode": True,
             "size_bytes": 3 * 1024**3},
            {"share_id": 1, "resource_id": 7, "title": "Show.S01E07.2160p.WEB-DL.H265.HDR10.mkv",
             "season_number": 1, "episode_numbers": [7], "safe_single_episode": True,
             "size_bytes": 8 * 1024**3},
        ]
        with patch.object(app, "tmdb_aired_episodes", return_value=aired), patch.object(
            app, "destination_episode_progress", return_value=progress
        ), patch.object(app, "transferred_episode_set", return_value=set()), patch.object(
            app, "dian_call", return_value={"data": []}
        ), patch.object(
            app, "normalize_supported_dian_resources", return_value=resources
        ), patch.object(
            app, "_dian_receive_exact_episode", return_value=True
        ) as receive, patch.object(app, "send_notifications"):
            result = app.auto_replenish_dian_follow(int(follow_id))
        self.assertEqual(result["transferred"], [[1, 8]])
        self.assertEqual(result["washed"], [[1, 7]])
        self.assertEqual(receive.call_count, 2)

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
