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
    def __init__(self, payload, status=200, headers=None, text=""):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.ok = 200 <= status < 300
        self.text = text

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

    def test_largest_missing_file_is_selected_with_subtitle(self):
        items = [
            {"_share_name": "S01E110.small.mkv", "_share_id": "small", "s": 100, "_share_is_dir": False},
            {"_share_name": "S01E110.large.mkv", "_share_id": "large", "s": 900, "_share_is_dir": False},
            {"_share_name": "S01E110.zh.ass", "_share_id": "sub", "s": 10, "_share_is_dir": False},
            {"_share_name": "S01E111.mkv", "_share_id": "111", "s": 800, "_share_is_dir": False},
            {"_share_name": "poster.jpg", "_share_id": "poster", "s": 1, "_share_is_dir": False},
        ]
        selected, episodes = app.select_largest_missing_episode_files(
            items,
            {110, 111},
        )
        self.assertEqual(episodes, {110, 111})
        self.assertEqual(
            {item["_share_id"] for item in selected},
            {"large", "sub", "111"},
        )

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

    def test_client_reads_same_origin_media_page_through_fixed_proxy(self):
        session = FakeSession(
            [
                FakeResponse(
                    {},
                    text='<script>"target_key":"tv:5670"</script>',
                )
            ]
        )
        client = HDHiveOpenAPI(
            api_key="secret",
            access_token="access",
            proxy_url="http://38.55.106.163:3128",
            session=session,
        )
        page = client.media_page(
            "https://hdhive.com/tv/f84222db3d0e11eea73a0242ac1b0003"
        )
        self.assertIn("tv:5670", page)
        self.assertEqual(
            session.calls[0][1],
            "https://hdhive.com/tv/f84222db3d0e11eea73a0242ac1b0003",
        )
        self.assertEqual(
            session.calls[0][2]["proxies"]["https"],
            "http://38.55.106.163:3128",
        )

    def test_client_rejects_external_media_page(self):
        session = FakeSession([])
        client = HDHiveOpenAPI(
            api_key="secret",
            access_token="access",
            session=session,
        )
        with self.assertRaises(ValueError):
            client.media_page("https://example.com/tv/not-hdhive")
        self.assertEqual(session.calls, [])

    def test_client_checkin_maps_normal_and_lucky_modes(self):
        session = FakeSession(
            [
                FakeResponse({"success": True, "data": {"points": 8}}),
                FakeResponse({"success": True, "data": {"points": 16}}),
            ]
        )
        client = HDHiveOpenAPI(
            api_key="secret", access_token="access", session=session
        )
        client.checkin()
        client.checkin(is_gambler=True)
        self.assertTrue(session.calls[0][1].endswith("/api/open/checkin"))
        self.assertEqual(session.calls[0][2]["json"], {})
        self.assertEqual(session.calls[1][2]["json"], {"is_gambler": True})

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

    def test_hdhive_resource_prefers_detailed_resource_title(self):
        resource = app.normalize_hdhive_resource(
            {
                "slug": "abc",
                "title": "仙逆 (2023)",
                "resource_title": "仙逆 S01E150-S01E151 4K WEB-DL",
                "share_size": "4.3GB",
            }
        )
        self.assertEqual(resource["title"], "仙逆 S01E150-S01E151 4K WEB-DL")
        self.assertEqual(resource["episode_numbers"], [150, 151])

    def test_supported_hdhive_resources_only_keep_115_and_offline_links(self):
        resources = app.normalize_supported_hdhive_resources(
            [
                {"slug": "115", "title": "第151集", "pan_type": "115"},
                {
                    "slug": "ed2k",
                    "title": "第151集",
                    "url": "ed2k://|file|episode.mkv|123|HASH|/",
                },
                {
                    "slug": "magnet",
                    "title": "第151集",
                    "offline_type": "magnet",
                },
                {"slug": "baidu", "title": "全集", "pan_type": "baiDu"},
                {"slug": "quark", "title": "全集", "pan_type": "quark"},
                {"slug": "aliyun", "title": "全集", "pan_type": "aliyun"},
            ]
        )

        self.assertEqual(
            [resource["slug"] for resource in resources],
            ["115", "ed2k", "magnet"],
        )
        self.assertEqual(
            [resource["share_type_label"] for resource in resources],
            ["115", "ED2K", "磁力"],
        )

    def test_subscription_target_accepts_documented_root_tv_id(self):
        target = app.hdhive_subscription_target(
            {
                "data": {
                    "tv_id": 77,
                    "media": {
                        "type": "tv",
                        "tmdb_id": 223911,
                        "name": "仙逆",
                    },
                }
            },
            223911,
            "tv",
        )
        self.assertEqual(target["target_id"], 77)
        self.assertEqual(target["target_key"], "tv:77")

    def test_subscription_target_accepts_explicit_target_key(self):
        target = app.hdhive_subscription_target(
            {
                "data": {
                    "target_id": 81,
                    "target_key": "movie:81",
                    "media": {
                        "type": "movie",
                        "tmdb_id": 550,
                        "name": "搏击俱乐部",
                    },
                }
            },
            550,
            "movie",
        )
        self.assertEqual(target["target_id"], 81)
        self.assertEqual(target["target_key"], "movie:81")

    def test_subscription_target_reads_server_rendered_media_page(self):
        page_html = (
            '<script>self.__next_f.push([1,"'
            '\\"target\\":{\\"target_type\\":\\"media_resource\\",'
            '\\"target_id\\":5670,\\"target_key\\":\\"tv:5670\\"},'
            '\\"item\\":{\\"media_type\\":\\"tv\\",'
            '\\"tv_id\\":5670,\\"tmdb_id\\":\\"223911\\"}'
            '"])</script>'
        )
        target = app.hdhive_subscription_target_from_page(
            page_html,
            223911,
            "tv",
            "仙逆",
        )
        self.assertEqual(target["target_id"], 5670)
        self.assertEqual(target["target_key"], "tv:5670")
        self.assertEqual(target["title"], "仙逆")

    def test_subscription_target_rejects_wrong_tmdb_media_page(self):
        page_html = (
            '{"target_type":"media_resource","target_id":5670,'
            '"target_key":"tv:5670","tmdb_id":"999"}'
        )
        with self.assertRaisesRegex(Exception, "TMDB"):
            app.hdhive_subscription_target_from_page(
                page_html,
                223911,
                "tv",
            )

    def test_hdhive_official_group_is_preferred_before_size(self):
        official = app.normalize_hdhive_resource(
            {
                "slug": "official",
                "title": "第233集",
                "share_size": "1.5G",
                "unlock_points": 0,
                "user": {"group_name": "影巢官组"},
            }
        )
        large_user_share = app.normalize_hdhive_resource(
            {
                "slug": "large",
                "title": "第233集",
                "share_size": "20G",
                "unlock_points": 2,
                "user": {"group_name": "普通用户"},
            }
        )

        ordered = sorted(
            [large_user_share, official],
            key=app.hdhive_resource_priority,
            reverse=True,
        )

        self.assertTrue(official["is_official_group"])
        self.assertTrue(official["vip_free"])
        self.assertEqual(ordered[0]["slug"], "official")

    def test_hdhive_size_is_preferred_before_pack_within_same_tier(self):
        larger_single = app.normalize_hdhive_resource(
            {
                "slug": "larger-single",
                "title": "第233集",
                "share_size": "20G",
                "unlock_points": 2,
            }
        )
        smaller_pack = app.normalize_hdhive_resource(
            {
                "slug": "smaller-pack",
                "title": "第220-233集",
                "share_size": "10G",
                "unlock_points": 2,
            }
        )

        ordered = sorted(
            [smaller_pack, larger_single],
            key=app.hdhive_resource_priority,
            reverse=True,
        )

        self.assertTrue(smaller_pack["is_pack"])
        self.assertEqual(ordered[0]["slug"], "larger-single")


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

    def test_disabled_follow_feature_always_reports_polling_off(self):
        with app.db() as connection:
            app.set_setting(connection, "hdhive_poll_enabled", "1")

        status = app.hdhive_public_status()

        self.assertFalse(app.FOLLOW_FEATURE_ENABLED)
        self.assertFalse(status["poll_enabled"])

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

    def test_native_subscription_resolves_media_page_target_and_caches_it(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 132, 132, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
        app.cache_follow_resources(
            follow_id,
            "hdhive",
            [
                {
                    "slug": "resource-slug",
                    "media_url": (
                        "https://hdhive.com/tv/"
                        "f84222db3d0e11eea73a0242ac1b0003"
                    ),
                    "media_slug": "f84222db3d0e11eea73a0242ac1b0003",
                }
            ],
        )

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append((method, args, kwargs))
            if method == "share":
                return {
                    "data": {
                        "slug": "resource-slug",
                        "media": {
                            "type": "tv",
                            "tmdb_id": 223911,
                            "name": "仙逆",
                        },
                    }
                }
            if method == "create_subscription":
                self.assertEqual(kwargs["target_id"], 5670)
                self.assertEqual(kwargs["target_key"], "tv:5670")
                return {"data": {"id": 55, "target_key": "tv:5670"}}
            raise AssertionError(method)

        page_html = (
            '<script>self.__next_f.push([1,"'
            '\\"target\\":{\\"target_type\\":\\"media_resource\\",'
            '\\"target_id\\":5670,\\"target_key\\":\\"tv:5670\\"},'
            '\\"item\\":{\\"media_type\\":\\"tv\\",'
            '\\"tv_id\\":5670,\\"tmdb_id\\":\\"223911\\"}'
            '"])</script>'
        )
        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(
                app,
                "hdhive_media_page",
                return_value=page_html,
            ) as media_page:
                result = asyncio.run(
                    app.create_hdhive_follow_subscription(
                        follow_id,
                        FakeRequest({"slug": "resource-slug"}),
                        self.admin_token,
                    )
                )
                app.bind_hdhive_follow_subscription(
                    follow_id,
                    "resource-slug",
                )

        self.assertTrue(result["follow"]["hdhive_subscribed"])
        media_page.assert_called_once_with(
            "https://hdhive.com/tv/f84222db3d0e11eea73a0242ac1b0003"
        )
        self.assertEqual(
            [method for method, _args, _kwargs in calls].count("share"),
            1,
        )
        with app.db() as connection:
            cached = connection.execute(
                "SELECT target_id, target_key FROM hdhive_media_targets "
                "WHERE media_type = 'tv' AND tmdb_id = 223911"
            ).fetchone()
        self.assertEqual(cached["target_id"], 5670)
        self.assertEqual(cached["target_key"], "tv:5670")

    def test_removed_follow_is_not_returned_in_watchlist(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid

        result = app.delete_follow(follow_id, self.token)

        self.assertTrue(result["ok"])
        self.assertEqual(app.list_follows(self.token)["follows"], [])
        self.assertEqual(app.list_follows(self.admin_token)["follows"], [])

    def test_cancel_follow_also_deletes_native_hdhive_subscription(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, hdhive_subscription_id, "
                "created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 9001, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid

        with patch.object(
            app, "hdhive_call", return_value={"success": True}
        ) as hdhive:
            result = app.delete_follow(follow_id, self.token)

        self.assertTrue(result["ok"])
        hdhive.assert_called_once_with("delete_subscription", 9001)
        with app.db() as connection:
            row = connection.execute(
                "SELECT active, hdhive_subscription_id FROM tv_follows "
                "WHERE id = ?",
                (follow_id,),
            ).fetchone()
        self.assertEqual(row["active"], 0)
        self.assertIsNone(row["hdhive_subscription_id"])

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

    def test_hdhive_signin_records_result_and_sends_telegram(self):
        result = {
            "success": True,
            "message": "签到成功，获得 8 积分",
            "data": {
                "checked_in": True,
                "message": "签到成功，获得 8 积分",
                "points": 8,
            },
        }
        def fake_hdhive_call(method, *args, **kwargs):
            if method == "checkin":
                return result
            if method == "me":
                return {"data": {"points": 7164}}
            self.fail(f"unexpected HDHive call: {method}")

        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call) as call:
            with patch.object(app, "send_telegram") as telegram:
                returned = app.perform_hdhive_signin("lucky", source="auto")
        self.assertEqual(returned, result)
        self.assertEqual(
            call.call_args_list,
            [
                unittest.mock.call("checkin", is_gambler=True),
                unittest.mock.call("me"),
            ],
        )
        self.assertIn("影巢签到成功", telegram.call_args.args[0])
        self.assertIn("自动签到 · 运气签到", telegram.call_args.args[0])
        self.assertIn("本次签到积分：8", telegram.call_args.args[0])
        self.assertIn("当前总积分：7164", telegram.call_args.args[0])
        with app.db() as connection:
            self.assertEqual(
                app.setting(connection, "hdhive_last_signin_status"),
                "success",
            )
            self.assertEqual(
                app.setting(connection, "hdhive_last_signin_mode"),
                "lucky",
            )

    def test_hdhive_signin_settings_and_manual_route(self):
        with app.db() as connection:
            row = app.hdhive_oauth_row(connection)
            connection.execute(
                "UPDATE hdhive_oauth SET client_id = ?, app_secret_cipher = ?, "
                "scopes = ?, redirect_uri = ?, updated_at = ? WHERE id = 1",
                (
                    "app_test",
                    app.encrypt_secret("secret"),
                    app.HDHIVE_SCOPES,
                    "https://example.com/api/hdhive/oauth/callback",
                    app.now_iso(),
                ),
            )
        asyncio.run(
            app.update_hdhive_config(
                FakeRequest(
                    {
                        "signin_enabled": True,
                        "signin_time": "07:45",
                        "signin_mode": "normal",
                    }
                ),
                self.admin_token,
            )
        )
        status = app.hdhive_admin_status(self.admin_token)
        self.assertTrue(status["signin_enabled"])
        self.assertEqual(status["signin_time"], "07:45")
        self.assertEqual(status["signin_mode"], "normal")
        with patch.object(
            app,
            "perform_hdhive_signin",
            return_value={"message": "签到成功"},
        ) as signin:
            result = asyncio.run(
                app.hdhive_checkin(
                    FakeRequest({"mode": "lucky"}),
                    self.admin_token,
                )
            )
        self.assertTrue(result["ok"])
        signin.assert_called_once_with("lucky", source="manual")

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
                            "pan_type": "115",
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

    def test_follow_baseline_is_cleared_after_series_is_removed_from_emby(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 132, 132, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            app.set_setting(connection, "emby_url", "http://emby.test")
            app.set_setting(connection, "emby_api_key", "emby-key")

        with patch.object(app, "emby_library_tmdb_ids", return_value=set()) as library:
            row = app.refresh_follow_emby_baseline(follow_id)

        self.assertEqual(row["baseline_episode"], 0)
        library.assert_called_once_with(force=True)

    def test_admin_can_confirm_whole_transfer_with_existing_episodes(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                return {"state": True, "tasks": []}

            def clouddownload_task_add_url(self, _payload):
                return {"state": True, "task_id": "offline-1"}

        with app.db() as connection:
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 132, 132, ?, ?)",
                (admin_id, app.now_iso(), app.now_iso()),
            )

        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"url": "magnet:?xt=urn:btih:whole-pack"}},
        ):
            with patch.object(app, "p115_client", return_value=FakeP115()):
                with patch.object(app, "wait_for_p115_change", return_value=True):
                    with patch.object(app, "send_telegram"):
                        result = asyncio.run(
                            app.hdhive_transfer(
                                FakeRequest(
                                    {
                                        "slug": "whole-pack",
                                        "tmdb_id": 223911,
                                        "media_type": "tv",
                                        "title": "仙逆",
                                        "resource_title": "仙逆 S01E01-E149",
                                        "transfer_scope": "whole",
                                        "confirm_whole": True,
                                        "allow_existing": True,
                                    }
                                ),
                                self.admin_token,
                            )
                        )

        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["message"], "已加入115离线下载")


if __name__ == "__main__":
    unittest.main()
