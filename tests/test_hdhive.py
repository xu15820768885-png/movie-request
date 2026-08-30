import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app
from hdhive_openapi import HDHiveOpenAPI, TokenSet


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

        short_range = app.parse_episode_spec(
            "S01E01-E04 4K WEB-DL HQ 60FPS DV DTS5.1 HiveWeb"
        )
        repeated_season_range = app.parse_episode_spec(
            "S01E01-S01E04 4K DV DTS5.1 HiveWeb"
        )
        self.assertEqual(short_range["episode_numbers"], [1, 2, 3, 4])
        self.assertEqual(
            repeated_season_range["episode_numbers"],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            repeated_season_range["episode_label"],
            "第1季 · 第1–4集",
        )

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

    def test_episode_parser_expands_common_complete_pack_wording(self):
        for title in (
            "示例剧 33集全",
            "示例剧 共33集",
            "示例剧 33集完结",
            "示例剧 更新至33集",
            "示例剧 更新至第33集",
        ):
            with self.subTest(title=title):
                parsed = app.parse_episode_spec(title)
                self.assertEqual(parsed["episode_numbers"], list(range(1, 34)))
                self.assertEqual(parsed["episode_label"], "第1–33集")
                self.assertTrue(parsed["is_pack"])

        final_episode = app.parse_episode_spec("示例剧 第33集完结")
        self.assertEqual(final_episode["episode_numbers"], [33])
        self.assertEqual(final_episode["episode_label"], "第33集")

    def test_complete_pack_title_overrides_misleading_single_episode_label(self):
        for provider in ("hdhive", "dian"):
            with self.subTest(provider=provider):
                resource = app.canonical_resource(
                    {
                        "title": "示例剧 33集全",
                        "episode_label": "第33集",
                        "episode_numbers": [33],
                    },
                    provider,
                )
                self.assertEqual(resource["episode_numbers"], list(range(1, 34)))
                self.assertEqual(resource["episode_label"], "第1–33集")

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

    def test_season_aware_selection_does_not_mix_equal_episode_numbers(self):
        items = [
            {"_share_name": "Show.S01E01.mkv", "_share_id": "s1e1", "s": 2000},
            {"_share_name": "Show.S02E01.mkv", "_share_id": "s2e1", "s": 1000},
            {"_share_name": "Show.S02E01.zh.ass", "_share_id": "s2sub", "s": 10},
        ]

        selected, episode_keys = app.select_largest_missing_episode_files_by_season(
            items,
            {(2, 1)},
        )

        self.assertEqual(episode_keys, {(2, 1)})
        self.assertEqual(
            {item["_share_id"] for item in selected},
            {"s2e1", "s2sub"},
        )

    def test_auto_wash_only_accepts_direct_115_resources(self):
        direct = {"pan_type": "115", "is_offline": False}
        magnet = {"share_type_label": "磁力", "is_offline": True}
        ed2k = {"share_type_label": "ED2K", "is_offline": True}

        self.assertTrue(app.hdhive_resource_is_direct_115(direct))
        self.assertFalse(app.hdhive_resource_is_direct_115(magnet))
        self.assertFalse(app.hdhive_resource_is_direct_115(ed2k))

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

    def test_client_uses_documented_file_list_and_unread_count_paths(self):
        session = FakeSession(
            [
                FakeResponse({"success": True, "data": {"files": []}}),
                FakeResponse({"success": True, "data": {"unread_count": 2}}),
            ]
        )
        client = HDHiveOpenAPI(
            api_key="secret",
            access_token="access",
            session=session,
        )

        client.resource_file_list("resource-slug")
        client.unread_message_count(subscription_only=True)

        self.assertTrue(session.calls[0][1].endswith(
            "/api/open/resources/file-list/resource-slug"
        ))
        self.assertTrue(session.calls[1][1].endswith(
            "/api/open/messages/unread-count"
        ))
        self.assertEqual(
            session.calls[1][2]["params"], {"subscription_only": True}
        )

    def test_file_list_candidates_select_largest_file_for_episode(self):
        candidates = app.hdhive_file_episode_candidates(
            "resource-slug",
            {
                "data": {
                    "provider": "115",
                    "result_type": "files",
                    "files": [
                        {"name": "Series.S01E155.small.mkv", "size": 900_000_000},
                        {"name": "Series.S01E155.large.mkv", "size": 1_100_000_000},
                        {"name": "Series.S01E155.zh.ass", "size": 30_000},
                    ],
                }
            },
            1,
        )

        self.assertEqual(candidates[(1, 155)]["file_name"], "Series.S01E155.large.mkv")
        self.assertEqual(candidates[(1, 155)]["file_size"], 1_100_000_000)
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

    def test_hdhive_resource_uses_structured_fields_then_title_fallbacks(self):
        resources = app.normalize_supported_hdhive_resources(
            [
                {
                    "slug": "official-shape",
                    "resource_title": (
                        "S01E01-S01E04 4K WEB-DL HQ 60FPS DV DTS5.1 HiveWeb"
                    ),
                    "pan_type": "115",
                    "share_size": "18.64G",
                    "subtitle_language": "简中",
                    "resource": {
                        "video_codec": "HEVC",
                    },
                }
            ]
        )

        resource = resources[0]
        self.assertEqual(resource["episode_numbers"], [1, 2, 3, 4])
        self.assertEqual(resource["episode_label"], "第1季 · 第1–4集")
        self.assertEqual(resource["size_label"], "18.6 GB")
        self.assertEqual(resource["res"], "4K")
        self.assertEqual(resource["codec"], "H.265/HEVC")
        self.assertEqual(resource["hdr"], "Dolby Vision")
        self.assertEqual(resource["audio"], "DTS")
        self.assertEqual(resource["subtitle_label"], "简中")
        self.assertEqual(resource["field_sources"]["codec"], "api")
        self.assertEqual(resource["field_sources"]["hdr"], "title")

    def test_hdhive_resource_keeps_detailed_subtitle_language_and_type(self):
        resource = app.canonical_resource(
            app.normalize_hdhive_resource(
                {
                    "title": "S01 4K WEB-DL",
                    "subtitle_language": ["简中", "繁中"],
                    "subtitle_type": ["内封", "内嵌"],
                    "pan_type": "115",
                }
            ),
            "hdhive",
        )

        self.assertTrue(resource["chn_sub"])
        self.assertEqual(resource["subtitle_label"], "简中 · 繁中 · 内封 · 内嵌")

    def test_hdhive_resource_expands_subtitle_details_from_title(self):
        resource = app.canonical_resource(
            app.normalize_hdhive_resource(
                {
                    "title": "S01 4K WEB-DL DV DDP5.1 内封简繁+简/繁韩双语字幕",
                    "pan_type": "115",
                }
            ),
            "hdhive",
        )

        self.assertEqual(
            resource["subtitle_label"],
            "简中 · 繁中 · 简韩 · 繁韩 · 内封",
        )

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

    def test_hdhive_size_is_preferred_over_official_group(self):
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
        self.assertEqual(ordered[0]["slug"], "large")

    def test_hdhive_episode_completeness_is_preferred_before_size(self):
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
        self.assertEqual(ordered[0]["slug"], "smaller-pack")


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

    def test_follow_feature_reports_saved_polling_state(self):
        with app.db() as connection:
            app.set_setting(connection, "hdhive_poll_enabled", "1")

        status = app.hdhive_public_status()

        self.assertTrue(app.HDHIVE_MESSAGE_POLLING_ENABLED)
        self.assertTrue(status["poll_enabled"])

    def test_file_list_400_is_cached_instead_of_retried(self):
        error = app.HTTPException(400, "资源不存在或 slug 已失效")
        with patch.object(app, "hdhive_call", side_effect=error) as call:
            first = app.hdhive_cached_file_list("expired-slug")
            second = app.hdhive_cached_file_list("expired-slug")
            forced = app.hdhive_cached_file_list("expired-slug", force=True)

        self.assertIsNone(first[0])
        self.assertFalse(first[1])
        self.assertIsNone(second[0])
        self.assertTrue(second[1])
        self.assertTrue(forced[1])
        self.assertIn("slug", second[2])
        call.assert_called_once_with("resource_file_list", "expired-slug")

    def test_forced_file_list_refresh_bypasses_success_cache(self):
        first_payload = {"data": {"files": [{"name": "Show.S01E06.mkv"}]}}
        updated_payload = {"data": {"files": [{"name": "Show.S01E07.mkv"}]}}
        with patch.object(
            app,
            "hdhive_call",
            side_effect=[first_payload, updated_payload],
        ) as call:
            first = app.hdhive_cached_file_list("updated-slug")
            cached = app.hdhive_cached_file_list("updated-slug")
            forced = app.hdhive_cached_file_list("updated-slug", force=True)

        self.assertEqual(first[0], first_payload)
        self.assertEqual(cached[0], first_payload)
        self.assertTrue(cached[1])
        self.assertEqual(forced[0], updated_payload)
        self.assertFalse(forced[1])
        self.assertEqual(call.call_count, 2)

    def test_hdhive_429_preserves_retry_after_and_limit_scope(self):
        class LimitedClient:
            def messages(self):
                raise app.HDHiveOpenAPIError(
                    "请求过多",
                    status=429,
                    retry_after=777,
                    limit_scope="query",
                )

        with patch.object(app, "hdhive_client", return_value=LimitedClient()):
            with self.assertRaises(app.HTTPException) as raised:
                app.hdhive_call("messages")

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "777")
        self.assertIn("query", raised.exception.detail)

    def test_follow_progress_pair_resets_episode_when_season_advances(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, last_seen_season, last_seen_episode, "
                "created_at, updated_at) VALUES(?, 223911, '仙逆', 1, 155, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            app.update_follow_progress_pair(
                connection, follow_id, "last_seen", 2, 5
            )
            row = connection.execute(
                "SELECT last_seen_season, last_seen_episode FROM tv_follows WHERE id = ?",
                (follow_id,),
            ).fetchone()

        self.assertEqual(
            (row["last_seen_season"], row["last_seen_episode"]),
            (2, 5),
        )

    def test_existing_wash_window_stops_after_emby_when_disabled(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 154, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            opened = datetime.now(timezone.utc)
            connection.execute(
                "INSERT INTO hdhive_wash_episodes("
                "follow_id, season_number, episode_number, opened_at, closes_at, updated_at"
                ") VALUES(?, 1, 155, ?, ?, ?)",
                (
                    follow_id,
                    opened.isoformat(),
                    (opened + timedelta(hours=48)).isoformat(),
                    opened.isoformat(),
                ),
            )
            follow = connection.execute(
                "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
            ).fetchone()

        allowed = app.hdhive_wash_candidate_allowed(
            follow,
            {
                "season_number": 1,
                "episode_number": 155,
                "fingerprint": "fingerprint-155",
                "slug": "resource-slug",
            },
            {
                "wash_after_emby": False,
                "window_hours": 48,
                "lock_after_window": True,
                "max_transfers": 4,
                "reprocess_changed": True,
            },
            {(1, 155)},
        )

        self.assertFalse(allowed)

    def test_status_requires_reauthorization_until_new_scopes_are_in_token(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE hdhive_oauth SET client_id = 'app_test', "
                "app_secret_cipher = ?, access_token_cipher = ?, scopes = ?, "
                "authorized_scopes = ?, status = 'connected' WHERE id = 1",
                (
                    app.encrypt_secret("secret"),
                    app.encrypt_secret("old-token"),
                    app.HDHIVE_SCOPES,
                    "meta query unlock write vip",
                ),
            )
        self.assertTrue(app.hdhive_public_status()["reauthorization_required"])

        app.hdhive_save_tokens(
            TokenSet(
                access_token="new-token",
                refresh_token="refresh-token",
                scopes=app.HDHIVE_SCOPES.split(),
            )
        )

        status = app.hdhive_public_status()
        self.assertFalse(status["reauthorization_required"])
        self.assertTrue(status["subscription_authorized"])
        self.assertTrue(status["messages_authorized"])

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

    def test_reenabled_follow_replaces_episode_when_season_advances(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, active, baseline_season, baseline_episode, "
                "last_seen_season, last_seen_episode, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 0, 1, 155, 1, 155, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )
        detail = {
            "id": 223911,
            "name": "仙逆",
            "first_air_date": "2023-09-25",
        }
        with patch.object(app, "tmdb_get", return_value=detail):
            with patch.object(app, "destination_emby_ids", return_value={223911}):
                with patch.object(
                    app,
                    "destination_episode_progress",
                    return_value={
                        "emby_latest_season_number": 2,
                        "emby_latest_episode_number": 5,
                    },
                ):
                    result = asyncio.run(
                        app.create_follow(
                            FakeRequest({"tmdb_id": 223911, "media_type": "tv"}),
                            self.token,
                        )
                    )

        self.assertEqual(
            (
                result["follow"]["baseline_season"],
                result["follow"]["baseline_episode"],
            ),
            (2, 5),
        )
        self.assertEqual(
            (
                result["follow"]["last_seen_season"],
                result["follow"]["last_seen_episode"],
            ),
            (2, 5),
        )

    def test_follow_binds_the_requested_resource_without_auto_transfer(self):
        detail = {
            "id": 223911,
            "name": "仙逆",
            "original_name": "Renegade Immortal",
            "first_air_date": "2023-09-25",
            "poster_path": "/poster.jpg",
        }
        selected = {
            "slug": "selected-subscription-resource",
            "title": "仙逆 长期更新",
            "media_url": "https://hdhive.com/tv/example",
        }
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
        app.record_transfer(
            user_id=user_id,
            source="dian",
            resource_key="initial-resource",
            tmdb_id=223911,
            transfer_scope="manual",
            status="success",
            detail="已手动转存初始版本",
        )

        def fake_bind(follow_id, slug, resource):
            self.assertEqual(slug, selected["slug"])
            self.assertEqual(resource, selected)
            with app.db() as connection:
                connection.execute(
                    "UPDATE tv_follows SET hdhive_subscription_id = 55 "
                    "WHERE id = ?",
                    (follow_id,),
                )
                return connection.execute(
                    "SELECT * FROM tv_follows WHERE id = ?",
                    (follow_id,),
                ).fetchone()

        with patch.object(app, "tmdb_get", return_value=detail):
            with patch.object(app, "emby_library_tmdb_ids", return_value=set()):
                with patch.object(
                    app, "emby_series_episode_progress", return_value={}
                ):
                    with patch.object(
                        app,
                        "hdhive_call",
                        side_effect=AssertionError(
                            "create_follow must not search, unlock, or transfer"
                        ),
                    ):
                        with patch.object(
                            app,
                            "bind_hdhive_follow_subscription",
                            side_effect=fake_bind,
                        ) as bind:
                            result = asyncio.run(
                                app.create_follow(
                                    FakeRequest(
                                        {
                                            "tmdb_id": 223911,
                                            "media_type": "tv",
                                            "slug": selected["slug"],
                                            "resource": selected,
                                        }
                                    ),
                                    self.token,
                                )
                            )

        self.assertTrue(result["follow"]["hdhive_subscribed"])
        bind.assert_called_once()

    def test_follow_requires_initial_transfer_when_not_in_emby(self):
        detail = {
            "id": 223911,
            "name": "仙逆",
            "first_air_date": "2023-09-25",
        }
        with patch.object(app, "tmdb_get", return_value=detail):
            with patch.object(app, "emby_library_tmdb_ids", return_value=set()):
                with self.assertRaises(app.HTTPException) as error:
                    asyncio.run(
                        app.create_follow(
                            FakeRequest(
                                {
                                    "tmdb_id": 223911,
                                    "media_type": "tv",
                                    "slug": "resource-slug",
                                }
                            ),
                            self.token,
                        )
                    )
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("先手动转存初始版本", error.exception.detail)

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
        self.assertEqual(calls[1][2]["media_filters"], {"websites": ["115"]})

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

    def test_admin_watchlist_groups_same_title_followed_by_family_members(self):
        with app.db() as connection:
            member_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 9001, ?, ?)",
                [
                    (member_id, app.now_iso(), app.now_iso()),
                    (admin_id, app.now_iso(), app.now_iso()),
                ],
            )

        admin_items = app.list_follows(self.admin_token)["follows"]
        member_items = app.list_follows(self.token)["follows"]

        self.assertEqual(len(admin_items), 1)
        self.assertEqual(admin_items[0]["follower_count"], 2)
        self.assertEqual(set(admin_items[0]["follower_names"]), {"家人", "管理员"})
        self.assertEqual(len(admin_items[0]["follow_ids"]), 2)
        self.assertEqual(len(member_items), 1)
        self.assertNotIn("follower_count", member_items[0])

    def test_background_refresh_scans_shared_family_follow_once(self):
        with app.db() as connection:
            member_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 9001, ?, ?)",
                [
                    (member_id, app.now_iso(), app.now_iso()),
                    (admin_id, app.now_iso(), app.now_iso()),
                ],
            )
            app.set_setting(connection, "hdhive_auto_transfer", "0")

        with patch.object(
            app, "hdhive_call", return_value={"data": []}
        ) as hdhive:
            app.refresh_hdhive_subscribed_follows(force_file_lists=True)

        hdhive.assert_called_once_with("resources", "tv", 223911)

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
                        "offline_retry_cleanup": False,
                    }
                ),
                self.admin_token,
            )
        )
        status = app.hdhive_admin_status(self.admin_token)
        self.assertEqual(status["app_secret"], "secret")
        self.assertTrue(status["signin_enabled"])
        self.assertEqual(status["signin_time"], "07:45")
        self.assertEqual(status["signin_mode"], "normal")
        self.assertFalse(status["offline_retry_cleanup"])
        self.assertEqual(status["quiet_scan_interval"], 6 * 3600)
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
            app.set_setting(connection, "hdhive_auto_transfer", "0")

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append((method, args, kwargs))
            if method == "unread_message_count":
                return {"data": {"unread_count": 1}}
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
        self.assertTrue(message_call[2]["subscription_only"])
        self.assertEqual(message_call[2]["status"], "unread")
        read_call = next(call for call in calls if call[0] == "mark_messages_read")
        self.assertEqual(read_call[1][0], [9001])

    def test_subscription_message_targets_only_matching_native_follow(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            first = connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 101172, '吞噬星空', 77, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append(method)
            if method == "unread_message_count":
                return {"data": {"unread_count": 1}}
            if method == "messages":
                return {"data": [{"id": 9010, "subscription_id": 55}]}
            if method == "mark_messages_read":
                return {"success": True}
            raise AssertionError(method)

        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(
                app, "refresh_hdhive_subscribed_follows", return_value=0
            ) as refresh:
                self.assertEqual(app.poll_hdhive_follow_messages(), 1)

        refresh.assert_called_once_with(
            cycle_id="", force_file_lists=True, follow_ids={first}, strict=True
        )
        self.assertLess(calls.index("messages"), calls.index("mark_messages_read"))
        with app.db() as connection:
            row = connection.execute(
                "SELECT status, subscription_id FROM hdhive_message_log "
                "WHERE message_key = '9010'"
            ).fetchone()
        self.assertEqual(row["status"], "acknowledged")
        self.assertEqual(row["subscription_id"], 55)

    def test_subscription_message_stays_unread_until_failed_scan_retries(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )

        marked = []

        def fake_hdhive_call(method, *args, **kwargs):
            if method == "unread_message_count":
                return {"data": {"unread_count": 1}}
            if method == "messages":
                return {"data": [{"id": 9011, "subscription_id": 55}]}
            if method == "mark_messages_read":
                marked.extend(args[0])
                return {"success": True}
            raise AssertionError(method)

        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(
                app,
                "refresh_hdhive_subscribed_follows",
                side_effect=app.HTTPException(502, "影巢暂时失败"),
            ):
                with self.assertRaises(app.HTTPException):
                    app.poll_hdhive_follow_messages()
        self.assertEqual(marked, [])
        with app.db() as connection:
            failed = connection.execute(
                "SELECT status, attempt_count FROM hdhive_message_log "
                "WHERE message_key = '9011'"
            ).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempt_count"], 1)

        # A restart must preserve the failed item instead of treating it as an
        # old acknowledged row.
        app.init_db()
        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(
                app, "refresh_hdhive_subscribed_follows", return_value=0
            ):
                self.assertEqual(app.poll_hdhive_follow_messages(), 1)
        self.assertEqual(marked, [9011])
        with app.db() as connection:
            acknowledged = connection.execute(
                "SELECT status, attempt_count FROM hdhive_message_log "
                "WHERE message_key = '9011'"
            ).fetchone()
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertEqual(acknowledged["attempt_count"], 2)

    def test_processed_message_is_reconciled_when_remote_unread_count_is_zero(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )
            connection.execute(
                "INSERT INTO hdhive_message_log(message_key, status, "
                "attempt_count, processed_at, created_at) "
                "VALUES('9012', 'processed', 1, ?, ?)",
                (app.now_iso(), app.now_iso()),
            )
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"unread_count": 0}},
        ):
            self.assertEqual(app.poll_hdhive_follow_messages(), 0)
        with app.db() as connection:
            row = connection.execute(
                "SELECT status, acknowledged_at FROM hdhive_message_log "
                "WHERE message_key = '9012'"
            ).fetchone()
        self.assertEqual(row["status"], "acknowledged")
        self.assertTrue(row["acknowledged_at"])

    def test_follow_event_log_is_visible_to_admin(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, poster_path, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', '/xiani.jpg', ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
        app.log_hdhive_follow_event(
            "scan", "success", "影巢资源读取完成，共找到 8 个115候选",
            follow_id=follow_id, cycle_id="cycle-test",
            detail={"resource_count": 8},
        )

        result = app.hdhive_follow_events(movie_session=self.admin_token)

        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["title"], "仙逆")
        self.assertEqual(result["events"][0]["poster_path"], "/xiani.jpg")
        self.assertIn("/api/tmdb/image/w342/xiani.jpg", result["events"][0]["poster_url"])
        self.assertEqual(result["events"][0]["resource_status"], "ongoing")
        self.assertEqual(result["events"][0]["detail"]["resource_count"], 8)
        self.assertEqual(result["summary"]["success"], 1)
        with self.assertRaises(app.HTTPException) as denied:
            app.hdhive_follow_events(movie_session=self.token)
        self.assertEqual(denied.exception.status_code, 403)

    def test_real_subscription_message_content_is_visible_to_admin(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, "
                "hdhive_subscription_id, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            connection.execute(
                "INSERT INTO hdhive_message_log(message_key, event_type, "
                "payload_json, status, subscription_id, tmdb_id, "
                "attempt_count, acknowledged_at, created_at) "
                "VALUES('notice-1', 'resource_updated', ?, 'acknowledged', "
                "55, 223911, 1, ?, ?)",
                (
                    json.dumps(
                        {
                            "title": "订阅影视有新资源更新",
                            "content": "剧集《仙逆》更新了仙逆 (2023)",
                            "created_at": "2026-08-30T18:24:14+08:00",
                        },
                        ensure_ascii=False,
                    ),
                    app.now_iso(),
                    app.now_iso(),
                ),
            )

        result = app.hdhive_messages(movie_session=self.admin_token)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["headline"], "订阅影视有新资源更新")
        self.assertIn("《仙逆》", result["messages"][0]["content"])
        self.assertEqual(result["messages"][0]["follow_id"], follow_id)
        self.assertEqual(result["messages"][0]["status"], "acknowledged")

    def test_message_without_ids_matches_title_and_all_following_members(self):
        with app.db() as connection:
            admin_id = int(
                connection.execute(
                    "SELECT id FROM users WHERE username = 'admin'"
                ).fetchone()[0]
            )
            member_id = int(
                connection.execute(
                    "SELECT id FROM users WHERE username = 'member'"
                ).fetchone()[0]
            )
            first_id = connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, active, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 1, ?, ?)",
                (admin_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            second_id = connection.execute(
                "INSERT INTO tv_follows(user_id, tmdb_id, title, active, created_at, updated_at) "
                "VALUES(?, 223911, '仙逆', 1, ?, ?)",
                (member_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            connection.execute(
                "INSERT INTO hdhive_message_log(message_key, event_type, payload_json, "
                "status, created_at) VALUES('title-only', 'resource_updated', ?, "
                "'acknowledged', ?)",
                (
                    json.dumps(
                        {
                            "title": "订阅影视有新资源更新",
                            "content": "剧集《仙逆》更新了第156集",
                            "resource": {"quality": "2160p"},
                            "access_token": "must-not-leak",
                        },
                        ensure_ascii=False,
                    ),
                    app.now_iso(),
                ),
            )

        message = app.hdhive_messages(movie_session=self.admin_token)["messages"][0]

        self.assertEqual(message["follow_title"], "仙逆")
        self.assertEqual(set(message["follow_ids"]), {first_id, second_id})
        self.assertIn("管理员", message["display_name"])
        self.assertIn("家人", message["display_name"])
        self.assertIn(
            {"label": "resource.quality", "value": "2160p"},
            message["detail_fields"],
        )
        self.assertNotIn("must-not-leak", str(message["detail_fields"]))

    def test_management_log_includes_and_filters_completed_manual_unlocks(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
        app.log_hdhive_follow_event(
            "unlock", "success", "影巢资源解锁成功，正在提交到网盘",
            user_id=user_id, tmdb_id=119543, title="繁花",
            detail={"source": "hdhive", "resource_status": "completed"},
        )

        completed = app.hdhive_follow_events(
            resource_status="completed", movie_session=self.admin_token
        )
        ongoing = app.hdhive_follow_events(
            resource_status="ongoing", movie_session=self.admin_token
        )

        self.assertEqual(len(completed["events"]), 1)
        self.assertEqual(completed["events"][0]["title"], "繁花")
        self.assertEqual(completed["events"][0]["resource_status"], "completed")
        self.assertEqual(ongoing["events"], [])

    def test_follow_cycle_runs_six_hour_fallback_without_new_message(self):
        with patch.object(
            app, "poll_hdhive_follow_messages", return_value=0
        ) as messages:
            with patch.object(
                app, "refresh_hdhive_subscribed_follows", return_value=3
            ) as refresh:
                result = app.run_hdhive_follow_cycle(
                    authorized_scopes={"subscription", "messages"},
                    include_unsubscribed=False,
                    interval=1800,
                )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["changed_count"], 3)
        messages.assert_called_once_with(
            refresh_follows=True, cycle_id=result["cycle_id"]
        )
        refresh.assert_called_once_with(
            cycle_id=result["cycle_id"], force_file_lists=True
        )

    def test_follow_cycle_skips_recent_six_hour_fallback_without_new_message(self):
        with app.db() as connection:
            app.set_setting(connection, "hdhive_last_full_scan_at", app.now_iso())
        with patch.object(
            app, "poll_hdhive_follow_messages", return_value=0
        ) as messages:
            with patch.object(
                app, "refresh_hdhive_subscribed_follows", return_value=3
            ) as refresh:
                result = app.run_hdhive_follow_cycle(
                    authorized_scopes={"subscription", "messages"},
                    include_unsubscribed=False,
                    interval=900,
                )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["changed_count"], 0)
        messages.assert_called_once()
        refresh.assert_not_called()

    def test_follow_cycle_without_message_permissions_does_not_refresh_resources(self):
        with patch.object(app, "poll_hdhive_follow_messages") as messages:
            with patch.object(app, "refresh_hdhive_subscribed_follows") as refresh:
                result = app.run_hdhive_follow_cycle(
                    authorized_scopes={"query", "unlock"},
                    include_unsubscribed=True,
                    interval=900,
                )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["changed_count"], 0)
        messages.assert_not_called()
        refresh.assert_not_called()

    def test_follow_cycle_skips_recent_full_scan_after_targeted_message(self):
        with app.db() as connection:
            app.set_setting(connection, "hdhive_last_full_scan_at", app.now_iso())
        with patch.object(
            app, "poll_hdhive_follow_messages", return_value=1
        ) as messages:
            with patch.object(
                app, "refresh_hdhive_subscribed_follows", return_value=3
            ) as refresh:
                result = app.run_hdhive_follow_cycle(
                    authorized_scopes={"subscription", "messages"},
                    include_unsubscribed=False,
                    interval=900,
                )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["changed_count"], 0)
        messages.assert_called_once()
        refresh.assert_not_called()

    def test_emby_library_confirmation_is_added_to_follow_log(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            )

        app.log_follow_library_event(
            "p115", 223911, "Emby Webhook 已确认入库：S01E155",
            detail={"episode_numbers": [155]},
        )

        result = app.hdhive_follow_events(
            stage="library", movie_session=self.admin_token
        )
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["status"], "success")
        self.assertIn("S01E155", result["events"][0]["message"])

    def test_auto_wash_processes_changed_candidate_once_per_fingerprint(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "hdhive_subscription_id, created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 154, 154, 55, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid

        class FakeP115:
            def share_receive(self, _payload, **_kwargs):
                return {"state": True}

        calls = []

        def fake_hdhive_call(method, *args, **kwargs):
            calls.append((method, args, kwargs))
            if method == "resource_file_list":
                return {
                    "data": {
                        "provider": "115",
                        "result_type": "files",
                        "files": [
                            {"name": "XianNi.S01E155.mkv", "size": 1_100_000_000}
                        ],
                    }
                }
            if method == "unlock":
                return {"data": {"full_url": "https://115.com/s/example?password=abcd"}}
            raise AssertionError(method)

        tree = [
            {
                "_share_name": "XianNi.S01E155.mkv",
                "_share_id": "file-155",
                "_share_is_dir": False,
                "s": 1_100_000_000,
            }
        ]
        resource = {
            "slug": "resource-slug",
            "title": "仙逆 S01E155 4K",
            "pan_type": "115",
            "episode_numbers": [155],
            "size_gb": "1.1G",
        }
        with patch.object(app, "hdhive_call", side_effect=fake_hdhive_call):
            with patch.object(app, "destination_episode_progress", return_value={
                "emby_latest_season_number": 1,
                "emby_episode_numbers": {"1": list(range(1, 155))},
            }):
                with patch.object(app, "p115_client", return_value=FakeP115()):
                    with patch.object(app, "p115_share_tree", return_value=tree):
                        with patch.object(app, "p115_folder_snapshot", return_value={"before"}):
                            with patch.object(app, "wait_for_p115_change", return_value=True):
                                with patch.object(app, "send_notifications"):
                                    first = app.auto_wash_hdhive_follow(follow_id, [resource])
                                    second = app.auto_wash_hdhive_follow(follow_id, [resource])

        self.assertEqual(first["transferred"], [[1, 155]])
        self.assertEqual(second["transferred"], [])
        self.assertEqual([call[0] for call in calls].count("unlock"), 1)
        with app.db() as connection:
            episode = connection.execute(
                "SELECT process_count, last_file_size FROM hdhive_wash_episodes "
                "WHERE follow_id = ? AND season_number = 1 AND episode_number = 155",
                (follow_id,),
            ).fetchone()
        self.assertEqual(episode["process_count"], 1)
        self.assertEqual(episode["last_file_size"], 1_100_000_000)

    def test_manual_transfer_ignores_existing_library_episode_rules(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                return {"state": True, "tasks": []}

            def clouddownload_task_add_url(self, _payload):
                return {"state": True, "task_id": "offline-1"}

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
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"url": "magnet:?xt=urn:btih:manual-pack"}},
        ):
            with patch.object(app, "p115_client", return_value=FakeP115()):
                with patch.object(app, "wait_for_p115_change", return_value=True):
                    with patch.object(app, "send_telegram"):
                        result = asyncio.run(
                            app.hdhive_transfer(
                                FakeRequest(
                                    {
                                        "slug": "resource-slug",
                                        "tmdb_id": 101172,
                                        "media_type": "tv",
                                        "resource_title": "吞噬星空 S01E01-E233",
                                    }
                                ),
                                self.token,
                            )
                        )

        self.assertEqual(result["mode"], "offline")
        self.assertEqual(
            result["message"],
            "已加入115离线下载，完成后会出现在所选目录",
        )

    def test_duplicate_completed_offline_tasks_are_cleared_without_files_and_retried(self):
        info_hash = "ABCDEF1234567890"

        class FakeP115:
            def __init__(self):
                self.add_count = 0
                self.cleared = None

            def clouddownload_task_list(self, payload):
                return {"state": True, "tasks": []}

            def clouddownload_task_add_url(self, _payload):
                self.add_count += 1
                if self.add_count == 1:
                    return {
                        "state": False,
                        "message": "任务已存在，请勿输入重复的链接地址",
                    }
                return {"state": True, "task_id": "offline-new"}

            def clouddownload_task_clear(self, payload):
                self.cleared = payload
                return {"state": True}

        client = FakeP115()
        with patch.object(
            app,
            "hdhive_call",
            return_value={
                "data": {"url": f"magnet:?xt=urn:btih:{info_hash}"}
            },
        ):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "wait_for_p115_change", return_value=True):
                    with patch.object(app, "send_notifications_async"):
                        result = asyncio.run(
                            app.hdhive_transfer(
                                FakeRequest(
                                    {
                                        "slug": "updated-offline",
                                        "tmdb_id": 223911,
                                        "media_type": "tv",
                                        "resource_title": "测试剧 S01E01-E07",
                                    }
                                ),
                                self.token,
                            )
                        )

        self.assertEqual(client.add_count, 2)
        self.assertEqual(client.cleared, {"flag": 0})
        self.assertEqual(result["message"], "已保留原文件并重新加入115离线下载")

    def test_duplicate_offline_cleanup_can_be_disabled(self):
        class FakeP115:
            def clouddownload_task_list(self, _payload):
                return {"state": True, "tasks": []}

            def clouddownload_task_add_url(self, _payload):
                return {
                    "state": False,
                    "message": "任务已存在，请勿输入重复的链接地址",
                }

            def clouddownload_task_clear(self, _payload):
                raise AssertionError("disabled cleanup must not delete tasks")

        with app.db() as connection:
            app.set_setting(connection, "p115_offline_retry_cleanup", "0")
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"url": "magnet:?xt=urn:btih:disabled"}},
        ):
            with patch.object(app, "p115_client", return_value=FakeP115()):
                with self.assertRaises(app.HTTPException) as raised:
                    asyncio.run(
                        app.hdhive_transfer(
                            FakeRequest(
                                {
                                    "slug": "disabled-offline",
                                    "tmdb_id": 223911,
                                    "media_type": "tv",
                                    "resource_title": "测试剧 S01E01-E07",
                                }
                            ),
                            self.token,
                        )
                    )

        self.assertIn("任务已存在", raised.exception.detail)

    def test_manual_transfer_receives_full_share_and_allows_retry(self):
        class FakeP115:
            def fs_files(self, _payload):
                return {"state": True, "data": {"list": []}}

            def share_snap(self, *_args, **_kwargs):
                return {
                    "state": True,
                    "data": {"list": [{"fid": "101"}, {"cid": "202"}]},
                }

            def share_receive(self, payload, **_kwargs):
                self.received = payload
                return {"state": True}

        client = FakeP115()
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
        app.record_transfer(
            user_id=user_id,
            source="hdhive",
            resource_key="retry-share",
            tmdb_id=223911,
            transfer_scope="manual",
            status="success",
            detail="此前已转存",
        )

        with patch.object(
            app,
            "hdhive_call",
            return_value={
                "data": {
                    "url": "https://115.com/s/retryshare?password=abcd"
                }
            },
        ):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "wait_for_p115_change", return_value=True):
                    with patch.object(app, "send_telegram"):
                        result = asyncio.run(
                            app.hdhive_transfer(
                                FakeRequest(
                                    {
                                        "slug": "retry-share",
                                        "tmdb_id": 223911,
                                        "media_type": "tv",
                                        "resource_title": "仙逆 S01E01-E149",
                                    }
                                ),
                                self.token,
                            )
                        )

        self.assertEqual(result["mode"], "share")
        self.assertEqual(
            client.received,
            {"file_id": "101,202", "cid": "0"},
        )

    def test_follow_baseline_is_preserved_when_series_is_removed_from_emby(self):
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

        self.assertEqual(row["baseline_episode"], 132)
        self.assertEqual(row["current_emby_episode"], 0)
        library.assert_called_once_with(force=True)

    def test_follow_progress_endpoint_refreshes_stale_emby_episode(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            follow_id = connection.execute(
                "INSERT INTO tv_follows("
                "user_id, tmdb_id, title, baseline_episode, last_seen_episode, "
                "created_at, updated_at"
                ") VALUES(?, 223911, '仙逆', 151, 151, ?, ?)",
                (user_id, app.now_iso(), app.now_iso()),
            ).lastrowid
            app.set_setting(connection, "emby_url", "http://emby.test")
            app.set_setting(connection, "emby_api_key", "emby-key")

        with patch.object(
            app, "destination_emby_ids", return_value={223911}
        ) as library:
            with patch.object(
                app,
                "destination_episode_progress",
                return_value={
                    "emby_latest_season_number": 1,
                    "emby_latest_episode_number": 152,
                },
            ) as progress:
                result = app.follow_emby_progress(follow_id, self.token)

        self.assertEqual(result["current_emby_episode"], 152)
        follow = app.list_follows(self.token)["follows"][0]
        self.assertEqual(follow["baseline_episode"], 151)
        self.assertEqual(follow["current_emby_episode"], 152)
        library.assert_called_once_with("p115", force=True)
        progress.assert_called_once_with(
            "p115", 223911, known_in_library=True, force=True
        )

    def test_follow_progress_batch_shares_one_library_query(self):
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'member'"
            ).fetchone()[0]
            for tmdb_id, title in ((223911, "仙逆"), (101172, "吞噬星空")):
                connection.execute(
                    "INSERT INTO tv_follows("
                    "user_id, tmdb_id, title, baseline_episode, created_at, updated_at"
                    ") VALUES(?, ?, ?, 1, ?, ?)",
                    (user_id, tmdb_id, title, app.now_iso(), app.now_iso()),
                )

        with patch.object(
            app, "destination_emby_ids", return_value={223911, 101172}
        ) as library:
            with patch.object(
                app,
                "destination_episode_progress",
                side_effect=lambda _destination, tmdb_id, **_kwargs: {
                    "emby_latest_season_number": 2 if tmdb_id == 223911 else 1,
                    "emby_latest_episode_number": 5 if tmdb_id == 223911 else 233,
                },
            ) as progress:
                result = app.follows_emby_progress(self.token)

        self.assertEqual(len(result["progress"]), 2)
        library.assert_called_once_with("p115")
        self.assertEqual(progress.call_count, 2)
        values = {
            item["follow_id"]: (
                item["current_emby_season"], item["current_emby_episode"]
            ) for item in result["progress"]
        }
        self.assertIn((2, 5), values.values())
        self.assertIn((1, 233), values.values())

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
        self.assertEqual(
            result["message"],
            "已加入115离线下载，完成后会出现在所选目录",
        )


if __name__ == "__main__":
    unittest.main()
