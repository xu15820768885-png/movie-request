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
        with app.CACHE_LOCK:
            app.TMDB_RESPONSE_CACHE.clear()
            app.EMBY_LIBRARY_CACHE.update({"key": "", "expires": 0.0, "ids": set()})
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

    def test_details_include_story_cast_and_recommendations(self):
        details = {
            "id": 157336,
            "title": "星际穿越",
            "original_title": "Interstellar",
            "release_date": "2014-11-05",
            "overview": "一队探险家穿越虫洞。",
            "runtime": 169,
            "vote_average": 8.4,
            "vote_count": 36000,
            "genres": [{"name": "科幻"}, {"name": "剧情"}],
            "credits": {
                "cast": [{"name": "马修·麦康纳", "character": "库珀"}],
                "crew": [{"name": "克里斯托弗·诺兰", "job": "Director"}],
            },
            "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc"}]},
            "recommendations": {
                "results": [{"id": 27205, "title": "盗梦空间", "release_date": "2010-07-16"}]
            },
        }
        with patch.object(app, "tmdb_get", return_value=details) as tmdb:
            result = app.media_details("movie", 157336, self.token)
        tmdb.assert_called_once_with(
            "/movie/157336",
            {"language": "zh-CN", "append_to_response": "credits,videos,recommendations"},
        )
        self.assertEqual(result["runtime"], 169)
        self.assertEqual(result["directors"], ["克里斯托弗·诺兰"])
        self.assertEqual(result["cast"][0]["character"], "库珀")
        self.assertEqual(result["recommendations"][0]["title"], "盗梦空间")
        self.assertIn("youtube.com", result["trailer_url"])

    def test_tmdb_chart_filters_people_and_normalizes_media(self):
        payload = {
            "results": [
                {"id": 1, "media_type": "movie", "title": "电影", "release_date": "2026-01-01"},
                {"id": 2, "media_type": "tv", "name": "剧集", "first_air_date": "2025-01-01"},
                {"id": 3, "media_type": "person", "name": "演员"},
            ]
        }
        with patch.object(app, "tmdb_get", return_value=payload):
            result = app.charts("trending", self.token)
        self.assertEqual(result["title"], "本周热门")
        self.assertEqual([item["media_type"] for item in result["results"]], ["movie", "tv"])

    def test_tmdb_responses_are_cached(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"id": 157336, "title": "星际穿越"}

        with patch.object(app.requests, "get", return_value=FakeResponse()) as get:
            first = app.tmdb_get("/movie/157336", {"language": "zh-CN", "append_to_response": "credits"})
            second = app.tmdb_get("/movie/157336", {"language": "zh-CN", "append_to_response": "credits"})
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)

    def test_douban_chart_is_marked_for_tmdb_resolution(self):
        payload = {
            "subject_collection_items": [
                {
                    "id": "1292052",
                    "title": "肖申克的救赎",
                    "year": "1994",
                    "rating": {"value": 9.7, "count": 3000000},
                    "pic": {"large": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/test.jpg"},
                }
            ]
        }
        with patch.object(app, "douban_get", return_value=payload):
            result = app.charts("douban_movies", self.token)
        item = result["results"][0]
        self.assertEqual(result["title"], "豆瓣热门电影")
        self.assertEqual(item["source"], "douban")
        self.assertEqual(item["douban_id"], "1292052")
        self.assertEqual(item["tmdb_id"], 0)
        self.assertIn("/api/douban/poster", item["poster_url"])

    def test_douban_item_must_resolve_to_tmdb_before_requesting(self):
        subject = {
            "id": "1292052",
            "title": "肖申克的救赎",
            "original_title": "The Shawshank Redemption",
            "year": "1994",
        }
        tmdb = {"results": [{"id": 278, "title": "肖申克的救赎", "release_date": "1994-09-23"}]}
        with patch.object(app, "douban_get", return_value=subject):
            with patch.object(app, "tmdb_get", return_value=tmdb):
                result = app.resolve_douban("movie", "1292052", self.token)
        self.assertEqual(result, {"tmdb_id": 278, "media_type": "movie"})

    def test_douban_poster_proxy_rejects_non_douban_hosts(self):
        with self.assertRaises(app.HTTPException) as error:
            app.douban_poster("https://example.com/not-allowed.jpg", self.token)
        self.assertEqual(error.exception.status_code, 400)

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

    def test_telegram_notice_prompt_uses_next_plain_message(self):
        with patch.object(app, "send_telegram") as send:
            self.assertTrue(app.handle_telegram_message("/notice"))
            with app.db() as connection:
                self.assertEqual(app.setting(connection, "telegram_notice_pending"), "1")
            self.assertTrue(app.handle_telegram_message("今晚更新片库，请稍后再来看看。"))
        self.assertIn("片库公告已发布", send.call_args.args[0])
        self.assertEqual(
            app.site_notice(self.token)["text"],
            "今晚更新片库，请稍后再来看看。",
        )
        with app.db() as connection:
            self.assertEqual(app.setting(connection, "telegram_notice_pending"), "")

    def test_telegram_notice_direct_command_and_clear(self):
        with patch.object(app, "send_telegram"):
            self.assertTrue(app.handle_telegram_message("/notice 今晚新增 4K 资源"))
            self.assertEqual(app.site_notice(self.token)["text"], "今晚新增 4K 资源")
            self.assertTrue(app.handle_telegram_message("/clear_notice"))
        self.assertEqual(app.site_notice(self.token)["text"], "")

    def test_telegram_menu_contains_notice_commands(self):
        with patch.object(app, "telegram_request") as telegram:
            app.configure_telegram_menu()
        commands = telegram.call_args.args[1]["commands"]
        self.assertIn({"command": "notice", "description": "发布片库公告"}, commands)
        self.assertIn({"command": "clear_notice", "description": "清除片库公告"}, commands)


if __name__ == "__main__":
    unittest.main()
