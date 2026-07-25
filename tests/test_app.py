import asyncio
import hashlib
import json
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
            app.EMBY_LIBRARY_CACHE.update(
                {"key": "", "expires": 0.0, "ids": set(), "refreshing": False}
            )
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

    def test_tmdb_tv_status_is_normalized(self):
        ended = app.tmdb_media_item(
            {"id": 1, "name": "已播完", "status": "Ended"}, "tv", set()
        )
        ongoing = app.tmdb_media_item(
            {
                "id": 2,
                "name": "连载中",
                "status": "Returning Series",
                "last_episode_to_air": {"season_number": 2, "episode_number": 5},
                "next_episode_to_air": {"season_number": 2, "episode_number": 6},
                "seasons": [{"season_number": 2, "episode_count": 10}],
            },
            "tv",
            set(),
        )
        season_ended = app.tmdb_media_item(
            {
                "id": 4,
                "name": "本季播完",
                "status": "Returning Series",
                "last_episode_to_air": {"season_number": 1, "episode_number": 9},
                "next_episode_to_air": None,
                "seasons": [{"season_number": 1, "episode_count": 9}],
            },
            "tv",
            set(),
        )
        canceled = app.tmdb_media_item(
            {"id": 3, "name": "已取消", "status": "Canceled"}, "tv", set()
        )
        self.assertEqual(ended["series_status_label"], "全剧已完结")
        self.assertEqual(ongoing["series_status_label"], "第2季未完结")
        self.assertEqual(season_ended["series_status_label"], "第1季已完结")
        self.assertEqual(season_ended["series_status"], "season_ended")
        self.assertEqual(canceled["series_status_label"], "已取消")

    def test_search_does_not_wait_for_each_tv_detail(self):
        def fake_tmdb(path, _params):
            if path == "/search/multi":
                return {
                    "results": [
                        {"id": 10, "media_type": "tv", "name": "完结剧"},
                        {"id": 20, "media_type": "movie", "title": "电影"},
                    ]
                }
            self.fail(f"unexpected TMDB path: {path}")

        with patch.object(app, "tmdb_get", side_effect=fake_tmdb) as tmdb:
            with patch.object(app, "emby_library_tmdb_ids", return_value=set()) as emby:
                result = app.search("测试", self.token)
        self.assertEqual(tmdb.call_count, 1)
        emby.assert_called_once_with(prefer_cached=True)
        self.assertNotIn("series_status_label", result["results"][0])
        self.assertNotIn("series_status_label", result["results"][1])

    def test_emby_fast_path_returns_stale_ids_and_refreshes_in_background(self):
        with app.db() as connection:
            app.set_setting(connection, "emby_url", "http://nas:8096")
            app.set_setting(connection, "emby_api_key", "emby-key")
        cache_key = hashlib.sha256(b"http://nas:8096|emby-key").hexdigest()
        with app.CACHE_LOCK:
            app.EMBY_LIBRARY_CACHE.update(
                {
                    "key": cache_key,
                    "expires": 0.0,
                    "ids": {157336},
                    "refreshing": False,
                }
            )
        with patch.object(app, "Thread") as thread:
            result = app.emby_library_tmdb_ids(prefer_cached=True)
        self.assertEqual(result, {157336})
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["name"], "emby-cache-refresh")

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

    def test_admin_can_update_member_login_password_and_delete_account(self):
        member_token = "member-session-token"
        with app.db() as connection:
            member_id = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('member', '旧名称', ?, 'member', ?)",
                (app.hash_password("password123"), app.now_iso()),
            ).lastrowid
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
                (
                    hashlib.sha256(member_token.encode()).hexdigest(),
                    member_id,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                ),
            )
            timestamp = app.now_iso()
            connection.execute(
                "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, year, created_at, updated_at) "
                "VALUES(?, 1, 'movie', '待清理影片', '2026', ?, ?)",
                (member_id, timestamp, timestamp),
            )

        result = asyncio.run(
            app.update_user(
                member_id,
                FakeRequest({
                    "username": "new-member",
                    "display_name": "新名称",
                    "password": "newpassword123",
                }),
                self.token,
            )
        )
        self.assertTrue(result["ok"])
        with app.db() as connection:
            member = connection.execute(
                "SELECT username, display_name, password_hash FROM users WHERE id = ?",
                (member_id,),
            ).fetchone()
            sessions = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (member_id,)
            ).fetchone()[0]
        self.assertEqual(member["username"], "new-member")
        self.assertEqual(member["display_name"], "新名称")
        self.assertTrue(app.verify_password("newpassword123", member["password_hash"]))
        self.assertEqual(sessions, 0)

        self.assertTrue(app.delete_user(member_id, self.token)["ok"])
        with app.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users WHERE id = ?", (member_id,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM movie_requests WHERE user_id = ?", (member_id,)).fetchone()[0],
                0,
            )

    def test_admin_cannot_delete_admin_account(self):
        admin_id = app.session_user(self.token)["id"]
        with self.assertRaises(app.HTTPException) as error:
            app.delete_user(admin_id, self.token)
        self.assertEqual(error.exception.status_code, 400)

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

    def test_dian_resources_query_uses_tmdb_identity(self):
        response = {
            "data": {
                "list": [
                    {
                        "share_id": 11,
                        "resource_id": 22,
                        "title": "星际穿越 2160p",
                    }
                ]
            }
        }
        with patch.object(app, "dian_call", return_value=response) as call:
            result = app.dian_resources("movie", 157336, 0, self.token)
        self.assertEqual(result["resources"][0]["resource_id"], 22)
        call.assert_called_once_with(
            "list_shares",
            {
                "tmdb_id": 157336,
                "media_type": "movie",
                "page": 1,
                "size": 30,
                "sort": "hot",
            },
        )

    def test_dian_resources_flattens_nested_resource_details(self):
        response = {
            "data": {
                "list": [
                    {
                        "id": 11,
                        "title": "鬼谜东宫",
                        "source": "user_upload",
                        "resource": {
                            "id": 22,
                            "name": "鬼谜东宫 S01 2160p",
                            "resolution": "4K",
                            "video_codec": "HEVC",
                            "audio_info": "韩语 Atmos",
                            "dynamic_range": "HDR10",
                            "size": 32 * 1024 * 1024 * 1024,
                            "episode_summary": "全 8 集",
                            "heat": 88,
                            "tags": ["简中"],
                        },
                    }
                ]
            }
        }
        with patch.object(app, "dian_call", return_value=response):
            resource = app.dian_resources("tv", 279323, None, self.token)["resources"][0]
        self.assertEqual(resource["share_id"], 11)
        self.assertEqual(resource["resource_id"], 22)
        self.assertEqual(resource["title"], "鬼谜东宫 S01 2160p")
        self.assertEqual(resource["res"], "4K")
        self.assertEqual(resource["codec"], "HEVC")
        self.assertEqual(resource["audio"], "韩语 Atmos")
        self.assertEqual(resource["hdr"], "HDR10")
        self.assertEqual(resource["size_gb"], 32.0)
        self.assertEqual(resource["files"], "全 8 集")
        self.assertEqual(resource["episode_label"], "全 8 集")
        self.assertEqual(resource["hot"], 88)
        self.assertTrue(resource["chn_sub"])

    def test_dian_resource_derives_episode_range_from_title_and_count(self):
        resource = app.normalize_dian_resource(
            {
                "id": 11,
                "resource": {
                    "id": 22,
                    "name": "百花杀.2026.S01E01.第1集.2160p",
                    "file_count": 6,
                },
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第1–6集")

    def test_dian_resource_uses_openapi_v2_episode_csv_fields(self):
        resource = app.normalize_dian_resource(
            {
                "id": 11,
                "resource": {
                    "id": 22,
                    "name": "百花杀.2026.S01E01.2160p",
                    "seasons_csv": "1",
                    "episodes_csv": "1-6",
                    "episode_count": 6,
                },
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第1–6集")

    def test_dian_resource_uses_openapi_v2_share_episodes_field(self):
        resource = app.normalize_dian_resource(
            {
                "share_kind": "115",
                "media_type": "tv",
                "seasons_csv": "1",
                "episodes": "1-26",
                "episode_count": 26,
                "source": "user_upload",
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第1–26集")
        self.assertEqual(resource["share_type_label"], "115")

    def test_dian_resource_supports_array_episode_fields(self):
        resource = app.normalize_dian_resource(
            {
                "share_kind": "115",
                "seasons": [1],
                "episodes": [1, 2, 4],
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第1–2、4集")

    def test_dian_resource_labels_ed2k_offline_share(self):
        resource = app.normalize_dian_resource(
            {
                "share_kind": "offline",
                "offline_type": "ed2k",
                "url": "ed2k://|file|example.mkv|123|HASH|/",
            }
        )
        self.assertEqual(resource["share_type_label"], "ED2K")

    def test_dian_resource_formats_multiple_openapi_v2_seasons(self):
        resource = app.normalize_dian_resource(
            {
                "resource": {
                    "name": "示例剧集",
                    "seasons_csv": "1,2",
                    "episodes_csv": "1-12",
                    "episode_count": 24,
                }
            }
        )
        self.assertEqual(resource["episode_label"], "第1、2季 · 第1–12集")

    def test_dian_resource_lists_exact_episodes_from_openapi_file_list(self):
        resource = app.normalize_dian_resource(
            {
                "id": 11,
                "season": 1,
                "episode_count": 2,
                "file_list": [
                    "百花杀.2026.S01E03.2160p.mkv",
                    "百花杀.2026.S01E05.2160p.mkv",
                ],
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第3、5集")

    def test_dian_resource_compacts_exact_episode_range_from_nested_files(self):
        resource = app.normalize_dian_resource(
            {
                "resource": {
                    "name": "百花杀",
                    "metadata": {
                        "season": 1,
                        "episode_count": 3,
                        "file_list": [
                            {"file_name": "百花杀.S01E01.mkv"},
                            {"file_name": "百花杀.S01E02.mkv"},
                            {"file_name": "百花杀.S01E03.mkv"},
                        ],
                    },
                }
            }
        )
        self.assertEqual(resource["episode_label"], "第1季 · 第1–3集")

    def test_dian_tv_resources_do_not_default_to_specials(self):
        with patch.object(app, "dian_call", return_value={"data": {"rows": []}}) as call:
            app.dian_resources("tv", 88416, None, self.token)
        payload = call.call_args.args[1]
        self.assertNotIn("season", payload)

        with patch.object(app, "dian_call", return_value={"data": {"rows": []}}) as call:
            app.dian_resources("tv", 88416, 2, self.token)
        self.assertEqual(call.call_args.args[1]["season"], 2)

    def test_manual_dian_signin_accepts_selected_mode(self):
        with patch.object(app, "perform_dian_signin", return_value={"message": "签到成功"}) as signin:
            result = asyncio.run(
                app.dian_signin(FakeRequest({"mode": "lucky"}), self.token)
            )
        self.assertTrue(result["ok"])
        signin.assert_called_once_with("lucky", source="manual")

    def test_dian_signin_sends_telegram_record(self):
        with patch.object(app, "dian_call", return_value={"message": "获得 5 积分"}):
            with patch.object(app, "send_telegram") as send:
                result = app.perform_dian_signin("lucky", source="auto")
        self.assertEqual(result["message"], "获得 5 积分")
        self.assertIn("自动签到 · 运气签到", send.call_args.args[0])
        self.assertIn("获得 5 积分", send.call_args.args[0])
        with app.db() as connection:
            self.assertEqual(app.setting(connection, "dian_last_signin_status"), "success")

    def test_failed_dian_signin_sends_telegram_record_and_marks_day(self):
        with patch.object(
            app,
            "dian_call",
            side_effect=app.HTTPException(502, "接口不可用"),
        ):
            with patch.object(app, "send_telegram") as send:
                with self.assertRaises(app.HTTPException):
                    app.perform_dian_signin("normal", source="auto")
        self.assertIn("癫影签到失败", send.call_args.args[0])
        self.assertIn("接口不可用", send.call_args.args[0])
        with app.db() as connection:
            self.assertEqual(app.setting(connection, "dian_last_signin_status"), "failed")
            self.assertEqual(
                app.setting(connection, "dian_last_signin_day"),
                datetime.now().date().isoformat(),
            )

    def test_dian_settings_and_p115_target_are_persisted(self):
        payload = {
            "dian_base_url": "https://dian.example.com",
            "dian_api_key": "dys_secret",
            "dian_signin_enabled": True,
            "dian_signin_time": "07:45",
            "dian_signin_mode": "normal",
            "p115_app": "alipaymini",
            "p115_target_cid": "9988",
            "p115_target_name": "家庭影视",
        }
        with patch.object(app, "configure_telegram_menu"):
            asyncio.run(app.update_settings(FakeRequest(payload), self.token))
        settings = app.get_settings(self.token)
        self.assertTrue(settings["dian_configured"])
        self.assertEqual(settings["dian_key_prefix"], "dys_secr***")
        self.assertEqual(settings["dian_signin_time"], "07:45")
        self.assertEqual(settings["p115_target_cid"], "9988")
        self.assertEqual(settings["p115_app_name"], "115生活_支付宝小程序端")

    def test_dian_transfer_unlocks_and_receives_share(self):
        class FakeP115:
            def fs_files(self, _payload):
                items = [{"fid": "900", "n": "星际穿越"}] if hasattr(self, "received") else []
                return {"state": True, "data": {"list": items}}

            def share_snap(self, *_args, **_kwargs):
                self.share_url = _kwargs["share_url"]
                return {"state": True, "data": {"list": [{"fid": "101"}, {"cid": "202"}]}}

            def share_receive(self, payload, **_kwargs):
                self.received = payload
                return {"state": True}

        client = FakeP115()
        with app.db() as connection:
            app.set_setting(connection, "p115_target_cid", "7788")
        with patch.object(
            app,
            "dian_call",
            return_value={
                "already": True,
                "code": "ok",
                "new_balance": 10,
                "owner": False,
                "payload": "https://115cdn.com/s/example115code?password=abcd",
                "token_used": False,
                "unlock": True,
            },
        ) as dian:
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "send_telegram"):
                    result = asyncio.run(
                        app.dian_transfer(
                            FakeRequest(
                                {
                                    "share_id": 11,
                                    "resource_id": 22,
                                    "title": "星际穿越",
                                }
                            ),
                            self.token,
                        )
                    )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "share")
        dian.assert_called_once_with("unlock", {"share_id": 11, "resource_id": 22})
        self.assertEqual(
            client.share_url,
            "https://115cdn.com/s/example115code?password=abcd",
        )
        self.assertEqual(client.received, {"file_id": "101,202", "cid": "7788"})

    def test_dian_transfer_reads_payload_from_data_wrapper(self):
        class FakeP115:
            def fs_files(self, _payload):
                items = [{"fid": "900", "n": "鬼谜东宫"}] if hasattr(self, "received") else []
                return {"state": True, "data": {"list": items}}

            def share_snap(self, *_args, **kwargs):
                self.share_url = kwargs["share_url"]
                return {"state": True, "data": {"list": [{"fid": "101"}]}}

            def share_receive(self, payload, **_kwargs):
                self.received = payload
                return {"state": True}

        client = FakeP115()

        def fake_dian(method, payload):
            self.assertEqual(method, "unlock")
            self.assertEqual(payload, {"share_id": 11, "resource_id": 22})
            return {
                "data": {
                    "payload": "https://115cdn.com/s/example115code?password=abcd",
                }
            }

        with patch.object(app, "dian_call", side_effect=fake_dian):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "send_telegram"):
                    result = asyncio.run(
                        app.dian_transfer(
                            FakeRequest(
                                {
                                    "share_id": 11,
                                    "resource_id": 22,
                                    "title": "鬼谜东宫",
                                }
                            ),
                            self.token,
                        )
                    )
        self.assertEqual(result["mode"], "share")
        self.assertEqual(
            client.share_url,
            "https://115cdn.com/s/example115code?password=abcd",
        )

    def test_115cdn_url_is_recognized_as_share(self):
        self.assertTrue(
            app.is_115_share_url(
                "https://115cdn.com/s/example115code?password=abcd"
            )
        )

    def test_dian_offline_links_are_extracted_from_file_objects(self):
        links = app.extract_dian_transfer_links(
            {
                "files": [
                    {
                        "name": "episode01.mkv",
                        "offline_url": "ed2k://|file|episode01.mkv|123|hash1|/",
                    },
                    {
                        "name": "episode02.mkv",
                        "link": "ed2k://|file|episode02.mkv|456|hash2|/",
                    },
                ]
            }
        )
        self.assertEqual(
            links,
            [
                "ed2k://|file|episode01.mkv|123|hash1|/",
                "ed2k://|file|episode02.mkv|456|hash2|/",
            ],
        )

    def test_dian_offline_links_are_extracted_from_unlock_payload(self):
        links = app.extract_dian_transfer_links(
            {
                "code": "resource-code",
                "unlock": [
                    "ed2k://|file|episode01.mkv|123|hash1|/",
                    "ed2k://|file|episode02.mkv|456|hash2|/",
                ],
            }
        )
        self.assertEqual(len(links), 2)

    def test_dian_transfer_links_are_found_in_nested_or_json_payload(self):
        nested = {
            "payload": {
                "result": {
                    "content": "ed2k://|file|episode01.mkv|123|hash1|/",
                }
            }
        }
        encoded = {
            "payload": json.dumps(
                {"result": {"content": "https://115cdn.com/s/example115code"}}
            )
        }
        self.assertEqual(len(app.extract_dian_transfer_links(nested)), 1)
        self.assertEqual(len(app.extract_dian_transfer_links(encoded)), 1)

    def test_dian_transfer_submits_ed2k_as_offline_download(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                tasks = (
                    [{"info_hash": "abc123", "name": "episode.mkv"}]
                    if hasattr(self, "offline_payload")
                    else []
                )
                return {"state": True, "tasks": tasks}

            def clouddownload_task_add_url(self, payload):
                self.offline_payload = payload
                return {"state": True, "info_hash": "abc123"}

        client = FakeP115()
        with app.db() as connection:
            app.set_setting(connection, "p115_target_cid", "7788")
        with patch.object(
            app,
            "dian_call",
            return_value={"data": {"offline_url": "ed2k://|file|episode.mkv|123|hash|/"}},
        ):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "send_telegram"):
                    result = asyncio.run(
                        app.dian_transfer(
                            FakeRequest(
                                {
                                    "share_id": 11,
                                    "resource_id": 22,
                                    "title": "鬼谜东宫",
                                }
                            ),
                            self.token,
                        )
                    )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(
            client.offline_payload,
            {
                "url": "ed2k://|file|episode.mkv|123|hash|/",
                "wp_path_id": "7788",
            },
        )

    def test_dian_transfer_submits_multiple_offline_links(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                tasks = (
                    [{"info_hash": "abc123", "name": "鬼谜东宫"}]
                    if hasattr(self, "offline_payload")
                    else []
                )
                return {"state": True, "tasks": tasks}

            def clouddownload_task_add_urls(self, payload):
                self.offline_payload = payload
                return {"state": True}

        client = FakeP115()
        with app.db() as connection:
            app.set_setting(connection, "p115_target_cid", "7788")
        with patch.object(
            app,
            "dian_call",
            return_value={
                "data": {
                    "offline_urls": [
                        "ed2k://|file|episode01.mkv|123|hash1|/",
                        "ed2k://|file|episode02.mkv|456|hash2|/",
                    ]
                }
            },
        ):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "send_telegram"):
                    result = asyncio.run(
                        app.dian_transfer(
                            FakeRequest(
                                {
                                    "share_id": 11,
                                    "resource_id": 22,
                                    "title": "鬼谜东宫",
                                }
                            ),
                            self.token,
                        )
                    )
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(
            client.offline_payload,
            {
                "url[0]": "ed2k://|file|episode01.mkv|123|hash1|/",
                "url[1]": "ed2k://|file|episode02.mkv|456|hash2|/",
                "wp_path_id": "7788",
            },
        )

    def test_dian_transfer_rejects_false_positive_from_115(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                return {"state": True, "tasks": []}

            def clouddownload_task_add_url(self, _payload):
                return {"state": True, "info_hash": "abc123"}

        with patch.object(
            app,
            "dian_call",
            return_value={"data": {"offline_url": "ed2k://|file|episode.mkv|123|hash|/"}},
        ):
            with patch.object(app, "p115_client", return_value=FakeP115()):
                with patch.object(app, "wait_for_p115_change", return_value=False):
                    with self.assertRaises(app.HTTPException) as raised:
                        asyncio.run(
                            app.dian_transfer(
                                FakeRequest(
                                    {
                                        "share_id": 11,
                                        "resource_id": 22,
                                        "title": "鬼谜东宫",
                                    }
                                ),
                                self.token,
                            )
                        )
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("没有创建云下载任务", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
