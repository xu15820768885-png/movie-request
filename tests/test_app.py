import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
            app.TMDB_REFRESHING.clear()
            app.SETTINGS_CACHE.clear()
            app.RESOURCE_RESPONSE_CACHE.clear()
            app.RESOURCE_REQUEST_LOCKS.clear()
            app.EMBY_LIBRARY_CACHE.update(
                {"key": "", "expires": 0.0, "ids": set(), "refreshing": False}
            )
            app.EMBY_EPISODE_CACHE.clear()
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

    def test_tmdb_image_proxy_caches_image_on_nas(self):
        class FakeImageResponse:
            content = b"fake-jpeg"
            headers = {"Content-Type": "image/jpeg"}

            def raise_for_status(self):
                return None

        with patch.object(app.requests, "get", return_value=FakeImageResponse()) as get:
            first = app.tmdb_image("w500", "poster.jpg", self.token)
        with patch.object(
            app.requests,
            "get",
            side_effect=AssertionError("cached image should not be fetched again"),
        ):
            second = app.tmdb_image("w500", "poster.jpg", self.token)

        self.assertEqual(Path(first.path).read_bytes(), b"fake-jpeg")
        self.assertEqual(first.path, second.path)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(
            app.tmdb_image_proxy_url("/poster.jpg"),
            "/api/tmdb/image/w500/poster.jpg",
        )

    def test_integration_probe_returns_latency_without_failing_route(self):
        with patch.object(app, "probe_tmdb"):
            result = app.test_integration("tmdb", self.token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["service"], "tmdb")
        self.assertGreaterEqual(result["latency_ms"], 0)

        with patch.object(app, "probe_tmdb", side_effect=app.requests.Timeout):
            result = app.test_integration("tmdb", self.token)
        self.assertFalse(result["ok"])
        self.assertIn("代理", result["message"])

    def test_tmdb_get_uses_stale_cache_when_external_api_fails(self):
        class FakeJsonResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"id": 101172, "name": "吞噬星空"}

        with patch.object(app.TMDB_HTTP, "get", return_value=FakeJsonResponse()):
            expected = app.tmdb_get("/tv/101172", {"language": "zh-CN"})
        with app.CACHE_LOCK:
            key = next(iter(app.TMDB_RESPONSE_CACHE))
            _, stale_until, cached = app.TMDB_RESPONSE_CACHE[key]
            app.TMDB_RESPONSE_CACHE[key] = (0.0, stale_until, cached)
        with patch.object(app, "Thread") as thread:
            actual = app.tmdb_get("/tv/101172", {"language": "zh-CN"})

        self.assertEqual(actual, expected)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["name"], "tmdb-cache-refresh")

    def test_movie_watch_rejects_iso_and_bdmv_but_keeps_remux(self):
        self.assertFalse(
            app.hdhive_movie_resource_is_playable(
                {"title": "Movie.2026.2160p.UHD.BluRay.ISO"}
            )
        )
        self.assertFalse(
            app.hdhive_movie_resource_is_playable(
                {"title": "电影 4K 蓝光原盘 BDMV"}
            )
        )
        self.assertTrue(
            app.hdhive_movie_resource_is_playable(
                {"title": "Movie.2026.2160p.UHD.BluRay.REMUX.MKV"}
            )
        )

    def test_movie_watch_prefers_largest_playable_resource(self):
        resources = [
            {"title": "Movie 4K REMUX MKV", "size_gb": "82G"},
            {"title": "Movie 4K WEB-DL", "size_gb": "31G"},
            {"title": "Movie 4K UHD BluRay ISO", "size_gb": "95G"},
        ]
        playable = [
            item for item in resources
            if app.hdhive_movie_resource_is_playable(item)
        ]
        playable.sort(key=app.hdhive_movie_resource_priority, reverse=True)
        self.assertEqual(playable[0]["title"], "Movie 4K REMUX MKV")

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

    def test_emby_series_episode_progress_reads_latest_real_episode(self):
        with app.db() as connection:
            app.set_setting(connection, "emby_url", "http://nas:8096")
            app.set_setting(connection, "emby_api_key", "emby-key")

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        responses = [
            FakeResponse(
                {
                    "Items": [
                        {
                            "Id": "series-101172",
                            "ProviderIds": {"Tmdb": "101172"},
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "Items": [
                        {"ParentIndexNumber": 1, "IndexNumber": 232},
                        {"ParentIndexNumber": 1, "IndexNumber": 233},
                        {
                            "ParentIndexNumber": 1,
                            "IndexNumber": 234,
                            "IsMissing": True,
                        },
                    ]
                }
            ),
        ]
        with patch.object(app.requests, "get", side_effect=responses) as get:
            result = app.emby_series_episode_progress(101172)

        self.assertEqual(result["emby_latest_episode_number"], 233)
        self.assertEqual(result["emby_episode_label"], "已入库至第233集")
        self.assertEqual(result["emby_episode_numbers"], {"1": [232, 233]})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[0].kwargs["params"]["AnyProviderIdEquals"],
            "tmdb.101172",
        )
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["StartIndex"],
            0,
        )
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["Limit"],
            10000,
        )

    def test_emby_episode_progress_keeps_real_gaps_beyond_default_page_size(self):
        with app.db() as connection:
            app.set_setting(connection, "emby_url", "http://nas:8096")
            app.set_setting(connection, "emby_api_key", "emby-key")

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        missing = {7, 18, 29, 41, 55, 68, 79, 93, 108, 121}
        existing = [
            {
                "ParentIndexNumber": 1,
                "IndexNumber": episode,
            }
            for episode in range(1, 133)
            if episode not in missing
        ]
        responses = [
            FakeResponse(
                {
                    "Items": [
                        {
                            "Id": "series-223911",
                            "ProviderIds": {"Tmdb": "223911"},
                        }
                    ]
                }
            ),
            FakeResponse({"Items": existing, "TotalRecordCount": len(existing)}),
        ]
        with patch.object(app.requests, "get", side_effect=responses) as get:
            result = app.emby_series_episode_progress(223911)

        present = set(result["emby_episode_numbers"]["1"])
        self.assertEqual(len(present), 122)
        self.assertEqual(set(range(1, 133)) - present, missing)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["Limit"], 10000)

    def test_emby_episode_progress_has_a_separate_nonblocking_endpoint(self):
        with patch.object(app, "emby_library_tmdb_ids", return_value={101172}):
            with patch.object(
                app,
                "emby_series_episode_progress",
                return_value={
                    "emby_latest_episode_number": 232,
                    "emby_episode_label": "已入库至第232集",
                },
            ) as progress:
                result = app.emby_episode_progress(101172, self.token)
        self.assertEqual(result["emby_latest_episode_number"], 232)
        self.assertTrue(result["in_library"])
        progress.assert_called_once_with(
            101172,
            known_in_library=True,
            force=False,
        )

    def test_emby_episode_progress_explicitly_clears_removed_series(self):
        with patch.object(app, "emby_library_tmdb_ids", return_value=set()) as library:
            with patch.object(
                app,
                "emby_series_episode_progress",
                return_value={},
            ) as progress:
                result = app.emby_episode_progress(223911, self.token)

        self.assertFalse(result["in_library"])
        self.assertEqual(result["emby_latest_episode_number"], 0)
        self.assertEqual(result["emby_episode_label"], "")
        self.assertEqual(result["emby_episode_numbers"], {})
        library.assert_called_once_with(prefer_cached=True)
        progress.assert_called_once_with(
            223911,
            known_in_library=False,
            force=False,
        )

    def test_request_listing_never_waits_for_emby_sync(self):
        with patch.object(
            app,
            "sync_emby_requests",
            side_effect=AssertionError("request listing must stay database-only"),
        ):
            result = app.list_requests(self.token)
        self.assertEqual(result, {"requests": []})

    def test_resource_responses_are_short_cached_and_refreshable(self):
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": [], "meta": {"source": "test"}},
        ) as call:
            first = app.hdhive_resources("movie", 157336, self.token)
            second = app.hdhive_resources("movie", 157336, self.token)
            refreshed = app.hdhive_resources(
                "movie",
                157336,
                self.token,
                refresh=True,
            )
        self.assertEqual(first, second)
        self.assertEqual(refreshed["meta"], {"source": "test"})
        self.assertEqual(call.call_count, 2)

    def test_html_compression_and_small_poster_defaults_are_enabled(self):
        self.assertTrue(
            any(middleware.cls is app.GZipMiddleware for middleware in app.APP.user_middleware)
        )
        item = app.tmdb_media_item(
            {"id": 157336, "title": "星际穿越", "poster_path": "/poster.jpg"},
            "movie",
            set(),
        )
        self.assertEqual(item["poster_url"], "/api/tmdb/image/w342/poster.jpg")

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

    def test_tv_request_is_allowed_when_series_is_already_in_emby(self):
        canonical = {
            "id": 223911,
            "name": "仙逆",
            "first_air_date": "2023-09-25",
        }
        with patch.object(app, "tmdb_get", return_value=canonical):
            with patch.object(app, "emby_library_tmdb_ids", return_value={223911}):
                with patch.object(app, "send_telegram"):
                    result = asyncio.run(
                        app.create_request(
                            FakeRequest(
                                {
                                    "tmdb_id": 223911,
                                    "media_type": "tv",
                                }
                            ),
                            self.token,
                        )
                    )

        self.assertTrue(result["ok"])
        request = app.list_requests(self.token)["requests"][0]
        self.assertEqual(request["title"], "仙逆")
        self.assertEqual(request["media_type"], "tv")

    def test_emby_sync_removes_movie_and_tv_requests_after_ingest(self):
        timestamp = app.now_iso()
        with app.db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO movie_requests("
                "user_id, tmdb_id, media_type, title, created_at, updated_at"
                ") VALUES(?, 1001, 'movie', '示例电影', ?, ?)",
                (user_id, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO movie_requests("
                "user_id, tmdb_id, media_type, title, created_at, updated_at"
                ") VALUES(?, 1002, 'tv', '未完结剧集', ?, ?)",
                (user_id, timestamp, timestamp),
            )

        with patch.object(
            app,
            "emby_library_tmdb_ids",
            return_value={1001, 1002},
        ):
            removed = app.sync_emby_requests(force=True)

        with app.db() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM movie_requests "
                "WHERE tmdb_id IN (1001, 1002)"
            ).fetchone()[0]
        self.assertEqual(removed, 2)
        self.assertEqual(remaining, 0)

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

    def test_tv_details_do_not_wait_for_emby_episode_scan(self):
        details = {
            "id": 101172,
            "name": "吞噬星空",
            "first_air_date": "2020-11-29",
            "status": "Returning Series",
            "last_episode_to_air": {"season_number": 1, "episode_number": 233},
            "next_episode_to_air": {
                "season_number": 1,
                "episode_number": 234,
                "air_date": "2026-08-03",
            },
            "seasons": [{"season_number": 1, "episode_count": 234}],
            "credits": {"cast": [], "crew": []},
            "videos": {"results": []},
            "recommendations": {"results": []},
        }
        with patch.object(app, "tmdb_get", return_value=details):
            with patch.object(
                app,
                "emby_series_episode_progress",
                side_effect=AssertionError("详情主请求不应扫描 Emby 剧集"),
            ):
                result = app.media_details("tv", 101172, self.token)
        self.assertEqual(result["latest_episode_label"], "更新至第233集")
        self.assertNotIn("emby_episode_label", result)

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

        with patch.object(app.TMDB_HTTP, "get", return_value=FakeResponse()) as get:
            first = app.tmdb_get("/movie/157336", {"language": "zh-CN", "append_to_response": "credits"})
            second = app.tmdb_get("/movie/157336", {"language": "zh-CN", "append_to_response": "credits"})
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)

    def test_tmdb_cache_survives_memory_cache_clear(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"id": 157336, "title": "星际穿越"}

        with patch.object(app.TMDB_HTTP, "get", return_value=FakeResponse()):
            expected = app.tmdb_get("/movie/157336", {"language": "zh-CN"})
        with app.CACHE_LOCK:
            app.TMDB_RESPONSE_CACHE.clear()
        with patch.object(
            app.TMDB_HTTP,
            "get",
            side_effect=AssertionError("persistent cache should avoid a request"),
        ):
            actual = app.tmdb_get("/movie/157336", {"language": "zh-CN"})

        self.assertEqual(actual, expected)

    def test_tmdb_search_cache_is_fresh_for_one_hour(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

        started = app.time.monotonic()
        with patch.object(app.TMDB_HTTP, "get", return_value=FakeResponse()):
            app.tmdb_get(
                "/search/multi",
                {"query": "星际穿越", "language": "zh-CN"},
            )
        with app.CACHE_LOCK:
            fresh_until, stale_until, _ = next(
                iter(app.TMDB_RESPONSE_CACHE.values())
            )
        self.assertGreaterEqual(fresh_until - started, 3599)
        self.assertGreaterEqual(stale_until - started, 7 * 86400 - 1)

    def test_expired_disk_cache_returns_before_network_refresh(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{"id": 1, "media_type": "movie", "title": "缓存"}]}

        with patch.object(app.TMDB_HTTP, "get", return_value=FakeResponse()):
            expected = app.tmdb_get(
                "/search/multi",
                {"query": "缓存", "language": "zh-CN"},
            )
        with app.db() as connection:
            connection.execute(
                "UPDATE tmdb_cache SET expires_at = ?",
                (app.time.time() - 1,),
            )
        with app.CACHE_LOCK:
            app.TMDB_RESPONSE_CACHE.clear()
        with patch.object(app, "Thread") as thread:
            with patch.object(
                app.TMDB_HTTP,
                "get",
                side_effect=AssertionError("stale cache must return immediately"),
            ):
                actual = app.tmdb_get(
                    "/search/multi",
                    {"query": "缓存", "language": "zh-CN"},
                )

        self.assertEqual(actual, expected)
        thread.assert_called_once()

    def test_background_search_refresh_updates_local_catalog(self):
        payload = {
            "results": [
                {
                    "id": 157336,
                    "media_type": "movie",
                    "title": "星际穿越",
                }
            ]
        }

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch.object(app, "tmdb_get", return_value=payload):
            with patch.object(app, "Thread", ImmediateThread):
                app.schedule_tmdb_refresh(
                    "/search/multi",
                    {"query": "星际穿越"},
                    (3, 6),
                    "search-key",
                )

        self.assertEqual(app.local_tmdb_search("星际穿越")[0]["id"], 157336)
        self.assertNotIn("search-key", app.TMDB_REFRESHING)

    def test_tmdb_search_catalog_matches_chinese_and_original_title(self):
        app.cache_tmdb_search_catalog(
            [
                {
                    "id": 157336,
                    "media_type": "movie",
                    "title": "星际穿越",
                    "original_title": "Interstellar",
                    "popularity": 100,
                }
            ]
        )

        self.assertEqual(app.local_tmdb_search("星际 穿越")[0]["id"], 157336)
        self.assertEqual(app.local_tmdb_search("interstellar")[0]["id"], 157336)

    def test_search_returns_local_catalog_before_tmdb_refresh(self):
        app.cache_tmdb_search_catalog(
            [
                {
                    "id": 157336,
                    "media_type": "movie",
                    "title": "星际穿越",
                    "original_title": "Interstellar",
                    "release_date": "2014-11-07",
                }
            ]
        )
        with patch.object(
            app,
            "tmdb_get",
            side_effect=AssertionError("local search should not wait for TMDB"),
        ):
            result = app.search("星际穿越", self.token)

        self.assertEqual(result["source"], "local")
        self.assertTrue(result["refresh_recommended"])
        self.assertEqual(result["results"][0]["tmdb_id"], 157336)

    def test_search_refresh_forces_tmdb_and_updates_catalog(self):
        payload = {
            "results": [
                {
                    "id": 157336,
                    "media_type": "movie",
                    "title": "星际穿越",
                    "release_date": "2014-11-07",
                }
            ]
        }
        with patch.object(app, "tmdb_get", return_value=payload) as tmdb:
            result = app.search("星际穿越", self.token, refresh=True)

        self.assertEqual(result["source"], "tmdb")
        self.assertFalse(result["refresh_recommended"])
        self.assertTrue(tmdb.call_args.kwargs["force_refresh"])
        self.assertEqual(app.local_tmdb_search("星际穿越")[0]["id"], 157336)

    def test_cached_setting_avoids_reopening_database(self):
        self.assertEqual(app.cached_setting("tmdb_token"), "test-token")
        with patch.object(
            app,
            "db",
            side_effect=AssertionError("cached setting should not open SQLite"),
        ):
            self.assertEqual(app.cached_setting("tmdb_token"), "test-token")

    def test_multiple_cold_settings_share_one_database_read(self):
        with app.CACHE_LOCK:
            app.SETTINGS_CACHE.clear()
        real_db = app.db
        with patch.object(app, "db", side_effect=real_db) as database:
            values = app.cached_settings("emby_url", "emby_api_key")
            second = app.cached_settings("emby_url", "emby_api_key")

        self.assertEqual(values, {"emby_url": "", "emby_api_key": ""})
        self.assertEqual(second, values)
        self.assertEqual(database.call_count, 1)

    def test_search_database_indexes_exist(self):
        with app.db() as connection:
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        self.assertIn("tv_follow_user_search_idx", indexes)
        self.assertIn("resource_manual_success_tmdb_idx", indexes)

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
        self.assertEqual(ongoing["latest_episode_label"], "更新至第2季第5集")
        self.assertEqual(ongoing["next_episode_label"], "下一集为第2季第6集")
        self.assertEqual(season_ended["series_status_label"], "第1季已完结")
        self.assertEqual(season_ended["series_status"], "season_ended")
        self.assertEqual(canceled["series_status_label"], "已取消")

    def test_search_does_not_wait_for_each_tv_detail(self):
        def fake_tmdb(path, _params, timeout=15, force_refresh=False):
            if path == "/search/multi":
                self.assertEqual(timeout, (3, 6))
                self.assertFalse(force_refresh)
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
        self.assertEqual(
            set(result["timing_ms"]),
            {"tmdb", "emby", "database", "total"},
        )

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

    def test_admin_can_update_and_clear_site_notice_inline(self):
        updated = asyncio.run(
            app.update_site_notice(FakeRequest({"text": "  今晚更新 4K 资源  "}), self.token)
        )
        self.assertEqual(updated["text"], "今晚更新 4K 资源")
        self.assertEqual(app.site_notice(self.token)["text"], "今晚更新 4K 资源")

        cleared = asyncio.run(
            app.update_site_notice(FakeRequest({"text": ""}), self.token)
        )
        self.assertEqual(cleared["text"], "")
        self.assertEqual(app.site_notice(self.token)["text"], "")

    def test_member_cannot_update_site_notice(self):
        member_token = "member-notice-token"
        with app.db() as connection:
            member_id = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES('member-notice', '成员', ?, 'member', ?)",
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
        with self.assertRaises(app.HTTPException) as error:
            asyncio.run(
                app.update_site_notice(
                    FakeRequest({"text": "成员不能改"}), member_token
                )
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_telegram_menu_contains_notice_commands(self):
        with patch.object(app, "telegram_request") as telegram:
            app.configure_telegram_menu()
        commands = telegram.call_args.args[1]["commands"]
        self.assertIn({"command": "hdhive", "description": "影巢账号"}, commands)
        self.assertIn({"command": "notice", "description": "发布片库公告"}, commands)
        self.assertIn({"command": "clear_notice", "description": "清除片库公告"}, commands)

    def test_hdhive_account_summary_contains_points_and_quotas(self):
        responses = {
            "me": {
                "data": {
                    "id": 58395,
                    "level": "forever_vip",
                    "username": "sakura",
                    "checked_in_today": True,
                    "points": 7156,
                    "signin_days_total": 90,
                    "share_num": 3,
                    "weekly_free_quota": 400,
                    "weekly_free_quota_remaining": 400,
                    "weekly_free_quota_unlimited": False,
                    "bonus_quota": 0,
                    "is_blocked": False,
                }
            },
            "quota": {
                "data": {
                    "endpoint_limit": 1000,
                    "endpoint_remaining": 988,
                }
            },
        }
        with patch.object(
            app,
            "hdhive_call",
            side_effect=lambda method, *args, **kwargs: responses[method],
        ):
            summary = app.hdhive_account_summary()
        self.assertIn("sakura · ID 58395", summary)
        self.assertIn("永久 VIP", summary)
        self.assertIn("积分：7,156", summary)
        self.assertIn("周免费额度：400/400", summary)
        self.assertIn("OpenAPI 额度：988/1,000", summary)

    def test_telegram_hdhive_command_sends_account_summary(self):
        with patch.object(
            app,
            "hdhive_account_summary",
            return_value="🟠 影巢账号\n积分：7,156",
        ):
            with patch.object(app, "send_telegram") as telegram:
                self.assertTrue(app.handle_telegram_message("/hdhive"))
        telegram.assert_called_once_with("🟠 影巢账号\n积分：7,156")

    def test_dian_resources_query_uses_tmdb_identity(self):
        response = {
            "data": {
                "list": [
                    {
                        "share_id": 11,
                        "resource_id": 22,
                        "title": "星际穿越 2160p",
                        "share_kind": "115",
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
                        "share_kind": "115",
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
        self.assertEqual(resource["codec"], "H.265/HEVC")
        self.assertEqual(resource["audio"], "韩语 Atmos")
        self.assertEqual(resource["hdr"], "HDR10")
        self.assertEqual(resource["size_gb"], 32.0)
        self.assertEqual(resource["files"], "全 8 集")
        self.assertEqual(resource["episode_label"], "全 8 集")
        self.assertEqual(resource["hot"], 88)
        self.assertTrue(resource["chn_sub"])
        self.assertEqual(resource["size_label"], "32 GB")
        self.assertEqual(resource["subtitle_label"], "简中")
        self.assertEqual(resource["field_sources"]["codec"], "api")

    def test_dian_unknown_fields_are_labeled_without_false_guessing(self):
        resource = app.canonical_resource(
            app.normalize_dian_resource(
                {
                    "id": 11,
                    "share_kind": "115",
                    "title": "未标注版本",
                }
            ),
            "dian",
        )
        self.assertEqual(resource["codec"], "编码未标明")
        self.assertEqual(resource["subtitle_label"], "字幕未标明")
        self.assertEqual(resource["size_label"], "容量未知")
        self.assertEqual(resource["field_sources"]["codec"], "unknown")

    def test_dian_resources_only_keep_115_and_offline_links(self):
        response = {
            "data": {
                "list": [
                    {"id": 1, "title": "115", "share_kind": "115"},
                    {
                        "id": 2,
                        "title": "ED2K",
                        "share_kind": "offline",
                        "offline_type": "ed2k",
                    },
                    {"id": 3, "title": "夸克", "share_kind": "quark"},
                    {"id": 4, "title": "百度", "share_kind": "baidu"},
                ]
            }
        }
        with patch.object(app, "dian_call", return_value=response):
            resources = app.dian_resources("tv", 279323, None, self.token)[
                "resources"
            ]

        self.assertEqual(
            [resource["title"] for resource in resources],
            ["115", "ED2K"],
        )

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
        signin_result = {
            "message": "签到成功",
            "data": {
                "mode": "lucky",
                "award": 5,
                "new_balance": 128,
                "lucky_tier": "normal",
            },
        }
        with patch.object(app, "dian_call", return_value=signin_result):
            with patch.object(app, "send_telegram") as send:
                result = app.perform_dian_signin("lucky", source="auto")
        self.assertEqual(result["message"], "签到成功")
        self.assertIn("自动签到 · 运气签到", send.call_args.args[0])
        self.assertIn("本次签到积分：5", send.call_args.args[0])
        self.assertIn("当前总积分：128", send.call_args.args[0])
        with app.db() as connection:
            self.assertEqual(app.setting(connection, "dian_last_signin_status"), "success")

    def test_dian_signin_keeps_zero_or_negative_award(self):
        self.assertEqual(
            app.signin_points({"data": {"award": 0, "new_balance": 128}}),
            (0, 128),
        )
        self.assertEqual(
            app.signin_points({"data": {"award": -2, "new_balance": 126}}),
            (-2, 126),
        )

    def test_dian_signin_extracts_points_from_message(self):
        result = {"message": "签到成功，获得 8 积分，当前总积分 236"}
        self.assertEqual(app.signin_points(result), ("8", "236"))

    def test_dian_signin_supports_result_and_points_response(self):
        result = {"data": {"result": 6, "points": 242}}
        self.assertEqual(app.signin_points(result), (6, 242))

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
        self.assertEqual(settings["dian_api_key"], "dys_secret")
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

    def test_dian_transfer_builds_115_url_from_unlock_share_codes(self):
        links = app.extract_dian_transfer_links(
            {
                "payload": {
                    "file_extension": "mkv",
                    "file_id": "123",
                    "file_name": "仙逆",
                    "mode": "share",
                    "receive_code": "a1b2",
                    "share_code": "swexample",
                    "share_id": 11,
                    "share_kind": "115",
                }
            }
        )

        self.assertEqual(
            links,
            ["https://115.com/s/swexample?password=a1b2"],
        )

    def test_dian_transfer_accepts_structured_115_unlock_payload(self):
        class FakeP115:
            def fs_files(self, _payload):
                items = (
                    [{"fid": "900", "n": "仙逆"}]
                    if hasattr(self, "received")
                    else []
                )
                return {"state": True, "data": {"list": items}}

            def share_snap(self, *_args, **kwargs):
                self.share_url = kwargs["share_url"]
                return {"state": True, "data": {"list": [{"fid": "101"}]}}

            def share_receive(self, payload, **_kwargs):
                self.received = payload
                return {"state": True}

        client = FakeP115()
        with patch.object(
            app,
            "dian_call",
            return_value={
                "payload": {
                    "file_id": "123",
                    "file_list": [],
                    "file_name": "仙逆",
                    "mode": "share",
                    "receive_code": "a1b2",
                    "share_code": "swexample",
                    "share_id": 11,
                    "share_kind": "115",
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
                                    "title": "仙逆",
                                }
                            ),
                            self.token,
                        )
                    )

        self.assertEqual(result["mode"], "share")
        self.assertEqual(
            client.share_url,
            "https://115.com/s/swexample?password=a1b2",
        )
        self.assertEqual(client.received["file_id"], "101")

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

    def test_p115_sha1_and_nested_share_paths_are_normalized(self):
        sha1 = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"

        class FakeP115:
            def share_snap(self, cid, share_url):
                self.share_url = share_url
                if cid == 0:
                    return {"state": True, "data": [{"cid": "7", "n": "电影"}]}
                return {
                    "state": True,
                    "data": [{"fid": "8", "n": "正片.mkv", "sha": sha1, "s": "1024"}],
                }

        items = app.p115_share_tree(FakeP115(), "https://115.com/s/example")
        self.assertEqual(items[1]["_share_path"], ("电影", "正片.mkv"))
        self.assertEqual(app.p115_share_item_sha1(items[1]), sha1.lower())
        self.assertEqual(app.p115_share_item_size(items[1]), 1024)

    def test_pansave_proxy_supports_http_and_socks(self):
        self.assertEqual(
            app.pansave_proxy("http://user:pass@127.0.0.1:7890"),
            {
                "proxy_type": "http",
                "addr": "127.0.0.1",
                "port": 7890,
                "rdns": True,
                "username": "user",
                "password": "pass",
            },
        )
        self.assertEqual(
            app.pansave_proxy("socks5://127.0.0.1:1080")["proxy_type"],
            "socks5",
        )

    def test_pansave_api_hash_is_encrypted_at_rest_and_visible_to_admin(self):
        with app.db() as connection:
            app.set_setting(connection, "pansave_telegram_api_id", "123456")
            app.set_setting(
                connection,
                "pansave_telegram_api_hash_cipher",
                app.encrypt_secret("a" * 32),
            )
            app.set_setting(connection, "pansave_telegram_phone", "+8613800138000")
            app.set_setting(
                connection,
                "pansave_telegram_session_cipher",
                app.encrypt_secret("telegram-session"),
            )
            app.set_setting(connection, "pansave_telegram_authorized", "1")
        settings = app.get_settings(self.token)
        self.assertTrue(settings["pansave_configured"])
        self.assertTrue(settings["pansave_connected"])
        self.assertNotIn("pansave_telegram_session", settings)
        self.assertEqual(settings["pansave_telegram_api_hash"], "a" * 32)

    def test_new_accounts_keep_p115_default_and_can_be_assigned_p123(self):
        asyncio.run(
            app.create_user(
                FakeRequest({
                    "username": "cloud-user",
                    "display_name": "PanSave账号",
                    "password": "password123",
                    "storage_destination": "p123",
                }),
                self.token,
            )
        )
        asyncio.run(
            app.create_user(
                FakeRequest({
                    "username": "legacy-user",
                    "display_name": "115账号",
                    "password": "password123",
                }),
                self.token,
            )
        )
        users = {
            user["username"]: user
            for user in app.list_users(self.token)["users"]
        }
        self.assertEqual(users["cloud-user"]["storage_destination"], "p123")
        self.assertNotIn("p123_target_id", users["cloud-user"])
        self.assertEqual(users["legacy-user"]["storage_destination"], "p115")

    def test_hdhive_pansave_destination_is_fixed_by_logged_in_account(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE users SET storage_destination = 'p123' WHERE id = 1"
            )
        payload = {
            "slug": "resource-slug",
            "tmdb_id": 101,
            "media_type": "movie",
            "title": "测试电影",
            "resource_title": "测试电影 4K",
            "destination": "p115",
        }
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"full_url": "https://115.com/s/example"}},
        ):
            with patch.object(
                app,
                "deliver_to_pansave",
                new=AsyncMock(
                    return_value={"ok": True, "mode": "pansave", "message": "已发送"}
                ),
            ) as delivered:
                result = asyncio.run(
                    app.hdhive_transfer(FakeRequest(payload), self.token)
                )
        self.assertEqual(result["mode"], "pansave")
        delivered.assert_awaited_once()
        self.assertEqual(
            delivered.await_args.kwargs["share_url"],
            "https://115.com/s/example",
        )

    def test_hdhive_115_transfer_waits_until_target_folder_changes(self):
        class FakeP115:
            def fs_files(self, _payload):
                return {"state": True, "data": {"list": []}}

            def share_snap(self, *_args, **kwargs):
                self.share_url = kwargs["share_url"]
                return {"state": True, "data": {"list": [{"fid": "101"}]}}

            def share_receive(self, payload, **_kwargs):
                self.received = payload
                return {"state": True}

        client = FakeP115()
        payload = {
            "slug": "fast-resource",
            "tmdb_id": 101,
            "media_type": "movie",
            "title": "测试电影",
            "resource_title": "测试电影 4K",
        }
        with patch.object(
            app,
            "hdhive_call",
            return_value={"data": {"full_url": "https://115.com/s/example"}},
        ):
            with patch.object(app, "p115_client", return_value=client):
                with patch.object(app, "wait_for_p115_change", return_value=True) as wait:
                    with patch.object(app, "send_notifications_async") as notify:
                        result = asyncio.run(
                            app.hdhive_transfer(FakeRequest(payload), self.token)
                        )

        self.assertEqual(result["mode"], "share")
        self.assertEqual(result["message"], "已转存到115所选目录")
        self.assertEqual(client.received["file_id"], "101")
        wait.assert_called_once()
        notify.assert_called_once()

    def test_pansave_send_link_uses_saved_user_session(self):
        with app.db() as connection:
            app.set_setting(connection, "pansave_telegram_api_id", "123456")
            app.set_setting(
                connection,
                "pansave_telegram_api_hash_cipher",
                app.encrypt_secret("a" * 32),
            )
            app.set_setting(connection, "pansave_telegram_phone", "+8613800138000")
            app.set_setting(
                connection,
                "pansave_telegram_session_cipher",
                app.encrypt_secret("saved-session"),
            )
            app.set_setting(connection, "pansave_bot_username", "pansavenb_bot")

        class FakeMessage:
            id = 77

        class FakeClient:
            def __init__(self):
                self.sent = []
                self.disconnected = False

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def send_message(self, username, text):
                self.sent.append((username, text))
                return FakeMessage()

            async def disconnect(self):
                self.disconnected = True

        fake = FakeClient()
        with patch.object(app, "pansave_client", return_value=fake) as factory:
            result = asyncio.run(
                app.pansave_send_link("https://115.com/s/example?password=abcd")
            )
        factory.assert_called_once_with(123456, "a" * 32, "saved-session", "")
        self.assertEqual(
            fake.sent,
            [("pansavenb_bot", "https://115.com/s/example?password=abcd")],
        )
        self.assertTrue(fake.disconnected)
        self.assertEqual(result["message_id"], 77)

    def test_init_db_removes_legacy_direct_123_data(self):
        with app.db() as connection:
            app.set_setting(connection, "p123_client_secret", "legacy-secret")
            connection.execute(
                "CREATE TABLE p123_transfer_jobs(id TEXT PRIMARY KEY)"
            )
        app.init_db()
        with app.db() as connection:
            self.assertEqual(app.setting(connection, "p123_client_secret"), "")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='p123_transfer_jobs'"
            ).fetchone()
        self.assertIsNone(table)

    def test_dual_emby_credentials_are_selected_by_account_destination(self):
        with app.db() as connection:
            app.set_setting(connection, "emby_url", "http://emby-115")
            app.set_setting(connection, "emby_api_key", "key-115")
            app.set_setting(connection, "p123_emby_url", "http://emby-123")
            app.set_setting(connection, "p123_emby_api_key", "key-123")
        self.assertEqual(
            app.emby_credentials("p115"), ("http://emby-115", "key-115")
        )
        self.assertEqual(
            app.emby_credentials("p123"), ("http://emby-123", "key-123")
        )

    def test_emby_sync_updates_only_users_on_matching_storage(self):
        with app.db() as connection:
            p123_user = connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, "
                "storage_destination, created_at) VALUES(?, ?, ?, 'member', 'p123', ?)",
                ("cloud", "123账号", app.hash_password("password123"), app.now_iso()),
            ).lastrowid
            timestamp = app.now_iso()
            connection.execute(
                "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, "
                "created_at, updated_at) VALUES(1, 101, 'movie', '115电影', ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, "
                "created_at, updated_at) VALUES(?, 202, 'movie', '123电影', ?, ?)",
                (p123_user, timestamp, timestamp),
            )
        with patch.object(
            app,
            "emby_library_tmdb_ids",
            side_effect=lambda **kwargs: (
                {202} if kwargs.get("destination") == "p123" else {101}
            ),
        ):
            with patch.object(app, "send_notifications") as notify:
                removed = app.sync_emby_requests(force=True)
        self.assertEqual(removed, 2)
        self.assertEqual(notify.call_count, 2)
        with app.db() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM movie_requests"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_notification_fanout_sends_telegram_and_wecom(self):
        with patch.object(app, "send_telegram") as telegram:
            with patch.object(app, "send_wecom") as wecom:
                app.send_notifications("测试通知")
        telegram.assert_called_once_with("测试通知")
        wecom.assert_called_once_with("测试通知")

    def test_wecom_send_accepts_success_errcode_zero(self):
        with app.db() as connection:
            app.set_setting(connection, "wecom_agent_id", "1000005")
            app.set_setting(connection, "wecom_to_user", "@all")
        with patch.object(app, "wecom_request", return_value={"errcode": 0}) as request:
            self.assertTrue(app.send_wecom("企业微信测试"))
        payload = request.call_args.args[1]
        self.assertEqual(payload["touser"], "@all")
        self.assertEqual(payload["agentid"], 1000005)

    def test_wecom_signature_is_stable_and_secrets_are_visible_to_admin(self):
        self.assertEqual(
            app.wecom_signature("token", "1700000000", "nonce", "cipher"),
            app.wecom_signature("token", "1700000000", "nonce", "cipher"),
        )
        with app.db() as connection:
            app.set_setting(connection, "wecom_corp_id", "corp")
            app.set_setting(connection, "wecom_agent_id", "1000005")
            app.set_setting(connection, "wecom_secret", "top-secret")
            app.set_setting(connection, "wecom_callback_token", "callback-secret")
            app.set_setting(connection, "wecom_encoding_aes_key", "A" * 43)
        settings = app.get_settings(self.token)
        self.assertTrue(settings["wecom_configured"])
        self.assertEqual(settings["wecom_secret"], "top-secret")
        self.assertEqual(settings["wecom_callback_token"], "callback-secret")
        self.assertEqual(settings["wecom_encoding_aes_key"], "A" * 43)


if __name__ == "__main__":
    unittest.main()
