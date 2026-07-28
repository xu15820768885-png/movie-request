import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app
from hdhive_openapi import HDHiveOpenAPI


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class HDHiveFoundationTests(unittest.TestCase):
    def test_episode_parser_handles_single_and_range(self):
        single = app.parse_episode_spec("吞噬星空.S01E233.2160p.WEB-DL")
        self.assertEqual(single["season_number"], 1)
        self.assertEqual(single["episode_numbers"], [233])
        self.assertTrue(single["safe_single_episode"])

        pack = app.parse_episode_spec("吞噬星空 1-233不缺集 长期更新")
        self.assertEqual(pack["episode_start"], 1)
        self.assertEqual(pack["episode_end"], 233)
        self.assertTrue(pack["is_pack"])
        self.assertFalse(pack["safe_single_episode"])

    def test_episode_parser_does_not_treat_year_or_resolution_as_episode(self):
        parsed = app.parse_episode_spec("吞噬星空 2020 2160p HEVC")
        self.assertEqual(parsed["episode_numbers"], [])
        self.assertFalse(parsed["is_pack"])

    def test_episode_parser_supports_chinese_episode_marker(self):
        parsed = app.parse_episode_spec("吞噬星空 第233集 4K")
        self.assertEqual(parsed["episode_numbers"], [233])
        self.assertEqual(parsed["episode_label"], "第233集")

    def test_missing_episode_selection_only_returns_new_explicit_files(self):
        items = [
            {"_share_name": "S01E231.mkv", "_share_id": "231", "_share_is_dir": False},
            {"_share_name": "S01E232.mkv", "_share_id": "232", "_share_is_dir": False},
            {"_share_name": "S01E233.mkv", "_share_id": "233", "_share_is_dir": False},
            {"_share_name": "poster.jpg", "_share_id": "p", "_share_is_dir": False},
            {"_share_name": "Season 1", "_share_id": "d", "_share_is_dir": True},
        ]
        selected = app.select_missing_episode_files(items, baseline_episode=232)
        self.assertEqual([item["_share_id"] for item in selected], ["233"])

    def test_hdhive_secrets_are_encrypted_at_rest(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(app, "DATA_DIR", Path(temporary)):
                encrypted = app.encrypt_secret("super-secret")
                self.assertNotIn("super-secret", encrypted)
                self.assertEqual(app.decrypt_secret(encrypted), "super-secret")
                self.assertTrue((Path(temporary) / "hdhive-fernet.key").exists())

    def test_client_uses_only_explicit_fixed_proxy(self):
        session = FakeSession([FakeResponse({"success": True, "data": []})])
        client = HDHiveOpenAPI(
            api_key="secret",
            access_token="access",
            proxy_url="http://user:pass@38.55.106.163:3128",
            session=session,
        )
        result = client.resources("tv", 101172)
        self.assertTrue(result["success"])
        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.calls[0][2]["proxies"],
            {
                "http": "http://user:pass@38.55.106.163:3128",
                "https": "http://user:pass@38.55.106.163:3128",
            },
        )

    def test_client_preserves_meta_instead_of_stripping_wrapper(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "success": True,
                        "data": [{"slug": "abc"}],
                        "meta": {"total": 1},
                    }
                )
            ]
        )
        client = HDHiveOpenAPI(
            api_key="secret", access_token="access", session=session
        )
        result = client.resources("tv", 101172)
        self.assertEqual(result["meta"]["total"], 1)

    def test_client_reads_share_detail_for_subscription_target(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "success": True,
                        "data": {"slug": "abc", "media": {"id": 77, "type": "tv"}},
                    }
                )
            ]
        )
        client = HDHiveOpenAPI(
            api_key="secret", access_token="access", session=session
        )
        result = client.share("abc")
        self.assertEqual(result["data"]["media"]["id"], 77)
        self.assertTrue(session.calls[0][1].endswith("/api/open/shares/abc"))

    def test_hdhive_resource_normalization_marks_pack(self):
        resource = app.normalize_hdhive_resource(
            {
                "slug": "abc",
                "title": "吞噬星空 1-233不缺集",
                "video_resolution": ["4K"],
                "source": ["WEB-DL"],
                "subtitle_language": ["简中"],
                "pan_type": "115",
            }
        )
        self.assertEqual(resource["provider"], "hdhive")
        self.assertTrue(resource["is_pack"])
        self.assertEqual(resource["episode_end"], 233)
        self.assertTrue(resource["chn_sub"])


class HDHiveFollowRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(app, "DATA_DIR", Path(self.temporary.name))
        self.data_patch.start()
        app.DB_PATH = Path(self.temporary.name) / "test.db"
        app.init_db()
        self.token = "follow-session"
        self.admin_token = "admin-session"
        with app.db() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('member', '家人', ?, 'member', ?)",
                (app.hash_password("password123"), app.now_iso()),
            )
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
                (
                    hashlib.sha256(self.token.encode()).hexdigest(),
                    cursor.lastrowid,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                ),
            )
            admin = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('admin', '管理员', ?, 'admin', ?)",
                (app.hash_password("password123"), app.now_iso()),
            )
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
                (
                    hashlib.sha256(self.admin_token.encode()).hexdigest(),
                    admin.lastrowid,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                ),
            )

    def tearDown(self):
        self.data_patch.stop()
        self.temporary.cleanup()

    def test_follow_uses_emby_episode_as_baseline(self):
        detail = {
            "id": 101172,
            "name": "吞噬星空",
            "original_name": "Swallowed Star",
            "first_air_date": "2020-11-29",
            "poster_path": "/poster.jpg",
        }
        with patch.object(app, "tmdb_get", return_value=detail):
            with patch.object(app, "emby_library_tmdb_ids", return_value={101172}):
                with patch.object(
                    app,
                    "emby_series_episode_progress",
                    return_value={
                        "emby_latest_season_number": 1,
                        "emby_latest_episode_number": 232,
                    },
                ):
                    result = asyncio.run(
                        app.create_follow(
                            FakeRequest({"tmdb_id": 101172}), self.token
                        )
                    )
        self.assertEqual(result["follow"]["baseline_episode"], 232)
        self.assertEqual(app.list_follows(self.token)["follows"][0]["title"], "吞噬星空")

    def test_admin_can_bind_native_subscription_to_follow(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 101172, '吞噬星空', 232, 232, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append((method, args, kwargs))
            if method == "share":
                return {
                    "data": {
                        "slug": "resource-slug",
                        "title": "吞噬星空长期更新",
                        "media": {
                            "id": 77,
                            "type": "tv",
                            "tmdb_id": 101172,
                            "name": "吞噬星空",
                        },
                    }
                }
            if method == "create_subscription":
                return {"data": {"id": 55, "target_key": "tv:77"}}
            raise AssertionError(method)

        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            result = asyncio.run(
                app.create_hdhive_follow_subscription(
                    follow_id,
                    FakeRequest({"slug": "resource-slug"}),
                    self.admin_token,
                )
            )

        self.assertTrue(result["follow"]["hdhive_subscribed"])
        self.assertEqual(result["follow"]["hdhive_subscription_id"], 55)
        self.assertEqual(calls[1][0], "create_subscription")
        self.assertEqual(calls[1][2]["target_type"], "media_resource")
        self.assertEqual(calls[1][2]["target_key"], "tv:77")

    def test_member_cannot_bind_native_subscription_directly(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, created_at, updated_at"
                ") VALUES(?, 101172, '吞噬星空', ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
        with self.assertRaises(app.HTTPException) as error:
            asyncio.run(
                app.create_hdhive_follow_subscription(
                    follow_id,
                    FakeRequest({"slug": "resource-slug"}),
                    self.token,
                )
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_subscription_message_refreshes_episode_and_is_marked_read(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "hdhive_subscription_id, created_at, updated_at"
                ") VALUES(?, 101172, '吞噬星空', 232, 232, 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append((method, args, kwargs))
            if method == "messages":
                return {
                    "data": [
                        {
                            "id": 9001,
                            "type": "subscription",
                            "title": "订阅资源有更新",
                        }
                    ]
                }
            if method == "resources":
                return {
                    "data": [
                        {
                            "slug": "episode-233",
                            "title": "吞噬星空 S01E233 2160p WEB-DL",
                        }
                    ]
                }
            if method == "mark_messages_read":
                return {"success": True}
            raise AssertionError(method)

        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(app, "send_telegram") as telegram:
                created = app.poll_hdhive_follow_messages()

        self.assertEqual(created, 1)
        with app.db() as connection:
            follow = connection.execute(
                "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
            ).fetchone()
        self.assertEqual(follow["last_seen_episode"], 233)
        self.assertIn("第233集", follow["last_message"])
        self.assertTrue(telegram.called)
        message_call = next(call for call in calls if call[0] == "messages")
        self.assertEqual(message_call[2]["type"], "subscription")
        read_call = next(call for call in calls if call[0] == "mark_messages_read")
        self.assertEqual(read_call[1][0], [9001])

    def test_whole_transfer_is_rejected_when_library_already_has_episodes(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 101172, '吞噬星空', 232, 232, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )
        with self.assertRaises(app.HTTPException) as error:
            asyncio.run(
                app.hdhive_transfer(
                    FakeRequest(
                        {
                            "slug": "resource-slug",
                            "tmdb_id": 101172,
                            "media_type": "tv",
                            "transfer_scope": "whole",
                            "confirm_whole": True,
                        }
                    ),
                    self.token,
                )
            )
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("已有到第232集", error.exception.detail)


if __name__ == "__main__":
    unittest.main()
