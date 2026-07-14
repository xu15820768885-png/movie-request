import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class MovieRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DATA_DIR", Path(self.temporary.name))
        self.db_patch.start()
        app.DB_PATH = Path(self.temporary.name) / "test.db"
        app.init_db()
        self.token = "test-session-token"
        with app.db() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('admin', '管理员', ?, 'admin', ?)",
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
            app.set_setting(connection, "tmdb_token", "test-token")

    def tearDown(self):
        self.db_patch.stop()
        self.temporary.cleanup()

    def test_password_hash(self):
        encoded = app.hash_password("hello123")
        self.assertTrue(app.verify_password("hello123", encoded))
        self.assertFalse(app.verify_password("wrong123", encoded))

    def test_request_uses_canonical_tmdb_metadata(self):
        canonical = {
            "id": 157336,
            "title": "星际穿越",
            "original_title": "Interstellar",
            "release_date": "2014-11-05",
            "poster_path": "/poster.jpg",
            "overview": "TMDB 官方简介",
        }
        with patch.object(app, "tmdb_get", return_value=canonical) as tmdb:
            with patch.object(app, "send_telegram"):
                response = asyncio.run(
                    app.create_request(
                        FakeRequest({
                        "tmdb_id": 157336,
                        "media_type": "movie",
                        "title": "伪造片名",
                        "year": "2099",
                        }),
                        self.token,
                    )
                )
        self.assertTrue(response["ok"])
        tmdb.assert_called_once_with("/movie/157336", {"language": "zh-CN"})
        item = app.list_requests(self.token)["requests"][0]
        self.assertEqual(item["title"], "星际穿越")
        self.assertEqual(item["year"], "2014")
        self.assertEqual(item["overview"], "TMDB 官方简介")

    def test_rejects_mismatched_tmdb_record(self):
        with patch.object(app, "tmdb_get", return_value={"id": 1}):
            with self.assertRaises(app.HTTPException) as error:
                asyncio.run(
                    app.create_request(
                        FakeRequest({"tmdb_id": 157336, "media_type": "movie"}),
                        self.token,
                    )
                )
        self.assertEqual(error.exception.status_code, 400)

    def test_emby_url_accepts_root_or_emby_base(self):
        self.assertEqual(
            app.emby_api_url("http://nas:8096", "/Items"),
            "http://nas:8096/emby/Items",
        )
        self.assertEqual(
            app.emby_api_url("http://nas:8096/emby/", "/Items"),
            "http://nas:8096/emby/Items",
        )

    def test_request_rejected_when_already_in_emby(self):
        canonical = {
            "id": 157336,
            "title": "星际穿越",
            "release_date": "2014-11-05",
        }
        with patch.object(app, "tmdb_get", return_value=canonical):
            with patch.object(app, "emby_library_tmdb_ids", return_value={157336}):
                with self.assertRaises(app.HTTPException) as error:
                    asyncio.run(
                        app.create_request(
                            FakeRequest({"tmdb_id": 157336, "media_type": "movie"}),
                            self.token,
                        )
                    )
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Emby", error.exception.detail)

    def test_telegram_uses_configured_proxy(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": True}

        with app.db() as connection:
            app.set_setting(connection, "telegram_token", "bot-token")
            app.set_setting(connection, "telegram_proxy", "http://192.168.31.129:7890")
        with patch.object(app.requests, "post", return_value=FakeResponse()) as post:
            result = app.telegram_request("setMyCommands", {"commands": []})
        self.assertTrue(result["ok"])
        self.assertEqual(
            post.call_args.kwargs["proxies"],
            {
                "http": "http://192.168.31.129:7890",
                "https": "http://192.168.31.129:7890",
            },
        )

    def test_member_can_delete_only_own_request_and_admin_can_delete_any(self):
        member_token = "member-session-token"
        other_token = "other-session-token"
        with app.db() as connection:
            member = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('member', '成员', ?, 'member', ?)",
                (app.hash_password("password123"), app.now_iso()),
            ).lastrowid
            other = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('other', '其他成员', ?, 'member', ?)",
                (app.hash_password("password123"), app.now_iso()),
            ).lastrowid
            expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            connection.executemany(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
                [
                    (hashlib.sha256(member_token.encode()).hexdigest(), member, expires),
                    (hashlib.sha256(other_token.encode()).hexdigest(), other, expires),
                ],
            )
            timestamp = app.now_iso()
            own_request = connection.execute(
                "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, year, created_at, updated_at) "
                "VALUES(?, 1, 'movie', '自己的片', '2026', ?, ?)",
                (member, timestamp, timestamp),
            ).lastrowid
            admin_request = connection.execute(
                "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, year, created_at, updated_at) "
                "VALUES(?, 2, 'movie', '管理员可删', '2026', ?, ?)",
                (member, timestamp, timestamp),
            ).lastrowid
        with patch.object(app, "send_telegram"):
            with self.assertRaises(app.HTTPException) as error:
                app.delete_request(own_request, other_token)
            self.assertEqual(error.exception.status_code, 403)
            self.assertTrue(app.delete_request(own_request, member_token)["ok"])
            self.assertTrue(app.delete_request(admin_request, self.token)["ok"])
        with app.db() as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM movie_requests").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_telegram_messages_remove_old_reply_keyboard(self):
        with app.db() as connection:
            app.set_setting(connection, "telegram_token", "bot-token")
            app.set_setting(connection, "telegram_chat_id", "123456")
        with patch.object(app, "telegram_request") as telegram:
            app.send_telegram("测试")
        payload = telegram.call_args.args[1]
        self.assertEqual(payload["reply_markup"], {"remove_keyboard": True})


if __name__ == "__main__":
    unittest.main()
