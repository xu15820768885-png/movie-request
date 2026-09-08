import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests
import app
from guanying_client import GuanyingClient, GuanyingError


class CookieTests(unittest.TestCase):
    def test_cookie_roundtrip_preserves_scope_expiry_and_flags(self):
        source = GuanyingClient()
        source.session.cookies.set('session', 'root', domain='www.xn--wcv59z.com', path='/', secure=True, expires=int(time.time()) + 3600, rest={'HttpOnly': None, 'SameSite': 'Lax'})
        source.session.cookies.set('session', 'resource', domain='www.xn--wcv59z.com', path='/res')
        source.session.cookies.set('expired', 'old', domain='www.xn--wcv59z.com', expires=1)
        restored = GuanyingClient()
        restored.import_cookies(source.export_cookies())
        self.assertEqual([vars(c) for c in restored.session.cookies], [vars(c) for c in source.session.cookies if not c.is_expired()])
        self.assertEqual(len(list(restored.session.cookies)), 2)

    def test_legacy_cookie_renewal_does_not_send_old_and_new_values(self):
        client = GuanyingClient()
        client.import_cookies('{"session":"old"}')
        # Migration may be saved and reloaded before the next server renewal.
        restored = GuanyingClient()
        restored.import_cookies(client.export_cookies())
        response = requests.Response(); response.status_code = 200
        response.cookies.set('session', 'new', domain='.www.xn--wcv59z.com', path='/')
        def renew(*args, **kwargs):
            restored.session.cookies.update(response.cookies)
            return response
        with patch.object(restored.session, 'request', side_effect=renew):
            restored._raw('GET', '/')
        request = restored.session.prepare_request(requests.Request('GET', restored.base_url + '/'))
        self.assertEqual(request.headers['Cookie'], 'session=new')

    def test_gate_during_resource_fetch_is_error_not_empty_resources(self):
        client = GuanyingClient()
        response = requests.Response(); response.status_code = 200
        response._content = '浏览器安全验证'.encode(); response.encoding = 'utf-8'
        with patch.object(client, '_raw', return_value=response):
            with self.assertRaises(GuanyingError) as caught:
                client._checked('/res/downurl/tv/example')
        self.assertEqual(caught.exception.code, 'POW_FAILED')

    def test_unauthorized_resource_response_is_session_expiry(self):
        client = GuanyingClient()
        response = requests.Response(); response.status_code = 401; response._content = b''
        with patch.object(client, '_raw', return_value=response):
            with self.assertRaises(GuanyingError) as caught:
                client._checked('/res/downurl/tv/example')
        self.assertEqual(caught.exception.code, 'SESSION_EXPIRED')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in [('DATA_DIR', Path(self.temp.name)), ('DB_PATH', Path(self.temp.name) / 'test.db')]:
            patcher = patch.object(app, name, value); patcher.start(); self.addCleanup(patcher.stop)
        with app.CACHE_LOCK:
            app.SETTINGS_CACHE.clear()
        app.init_db()
        with app.db() as connection:
            app.guanying_row(connection)
            connection.execute("UPDATE guanying_session SET username_cipher=?, password_cipher=?, status='connected' WHERE id=1", (app.encrypt_secret('test-user'), app.encrypt_secret('test-password')))
        self.client = GuanyingClient()

    def run_operation(self, operation):
        with patch.object(app, 'guanying_client', return_value=self.client):
            return app.run_guanying_operation(operation)

    def test_valid_session_never_logs_in(self):
        with patch.object(self.client, 'login') as login:
            self.assertEqual(self.run_operation(lambda client: ['resource']), ['resource'])
        login.assert_not_called()

    def test_expired_session_logs_in_once_and_retries_original_query(self):
        operation = Mock(side_effect=[GuanyingError('expired', code='SESSION_EXPIRED'), ['resource']])
        with patch.object(self.client, 'authenticated', return_value=False), patch.object(self.client, 'login', return_value={'authenticated': True}) as login:
            self.assertEqual(self.run_operation(operation), ['resource'])
        login.assert_called_once_with('test-user', 'test-password')
        self.assertEqual(operation.call_count, 2)
        self.assertTrue(app.guanying_public_status()['authenticated'])

    def test_browser_recovery_uses_fresh_session_before_login(self):
        fresh = GuanyingClient()
        operation = Mock(side_effect=[GuanyingError('gate', code='POW_FAILED'), ['resource']])
        with patch.object(app, 'GuanyingClient', return_value=fresh), patch.object(fresh, 'authenticated', return_value=True), patch.object(fresh, 'login') as login:
            self.assertEqual(self.run_operation(operation), ['resource'])
        self.assertIs(operation.call_args_list[1].args[0], fresh)
        login.assert_not_called()

    def test_browser_recovery_can_relogin_like_manual_clean_login(self):
        fresh = GuanyingClient()
        operation = Mock(side_effect=[GuanyingError('gate', code='POW_FAILED'), ['resource']])
        with patch.object(app, 'GuanyingClient', return_value=fresh), patch.object(fresh, 'authenticated', return_value=False), patch.object(fresh, 'login', return_value={'authenticated': True}) as login:
            self.assertEqual(self.run_operation(operation), ['resource'])
        login.assert_called_once()

    def test_repeated_expiry_is_bounded_and_cooldown_survives_cache_reset(self):
        operation = Mock(side_effect=GuanyingError('expired', code='SESSION_EXPIRED'))
        with patch.object(self.client, 'authenticated', return_value=False), patch.object(self.client, 'login', return_value={'authenticated': True}) as login:
            with self.assertRaises(app.HTTPException):
                self.run_operation(operation)
            with app.CACHE_LOCK:
                app.SETTINGS_CACHE.clear()
            with self.assertRaises(app.HTTPException) as caught:
                self.run_operation(operation)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(operation.call_count, 2)
        login.assert_called_once()
        self.assertGreater(app.guanying_public_status()['recovery_retry_seconds'], 0)

    def test_captcha_stops_automatic_login_even_after_cooldown(self):
        operation = Mock(side_effect=GuanyingError('expired', code='SESSION_EXPIRED'))
        with patch.object(self.client, 'authenticated', return_value=False), patch.object(self.client, 'login', return_value={'authenticated': False, 'captcha_required': True}) as login:
            with self.assertRaises(app.HTTPException):
                self.run_operation(operation)
            with app.db() as connection:
                app.set_setting(connection, 'guanying_recovery_after', '0')
            with self.assertRaises(app.HTTPException):
                self.run_operation(operation)
        login.assert_called_once()
        self.assertEqual(app.guanying_public_status()['status'], 'captcha_required')

    def test_invalid_credentials_do_not_cause_endless_login(self):
        with patch.object(self.client, 'authenticated', return_value=False), patch.object(self.client, 'login', side_effect=GuanyingError('bad password', code='LOGIN_FAILED')) as login:
            with self.assertRaises(app.HTTPException):
                self.run_operation(Mock(side_effect=GuanyingError('expired', code='SESSION_EXPIRED')))
            with self.assertRaises(app.HTTPException):
                self.run_operation(Mock())
        login.assert_called_once()
        self.assertEqual(app.guanying_public_status()['status'], 'credentials_invalid')

    def test_network_error_does_not_trigger_password_login(self):
        with patch.object(self.client, 'login') as login:
            with self.assertRaises(app.HTTPException):
                self.run_operation(Mock(side_effect=requests.Timeout('timeout')))
        login.assert_not_called()

    def test_concurrent_queries_share_new_cookies_and_only_login_once(self):
        started, release = Event(), Event()
        attempts = []
        def authenticated(client):
            return client.session.cookies.get('session') == 'fresh'
        def login(client, username, password):
            attempts.append(1)
            client.session.cookies.set('session', 'fresh', domain='www.xn--wcv59z.com')
            return {'authenticated': True}
        def operation(client):
            if not authenticated(client):
                started.set()
                self.assertTrue(release.wait(5))
                raise GuanyingError('expired', code='SESSION_EXPIRED')
            return ['resource']
        with patch.object(GuanyingClient, 'authenticated', authenticated), patch.object(GuanyingClient, 'login', login), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(app.run_guanying_operation, operation)
            self.assertTrue(started.wait(5))
            second = executor.submit(app.run_guanying_operation, operation)
            release.set()
            self.assertEqual(first.result(5), ['resource'])
            self.assertEqual(second.result(5), ['resource'])
        self.assertEqual(len(attempts), 1)

    def test_manual_login_clears_cooldown_and_pending_captcha(self):
        with app.db() as connection:
            app.set_setting(connection, 'guanying_recovery_after', time.time() + 900)
            connection.execute("UPDATE guanying_session SET status='captcha_required' WHERE id=1")
        app.GUANYING_LOGIN_ATTEMPTS['obsolete'] = {'created': time.time()}
        with patch.object(GuanyingClient, 'login', return_value={'authenticated': True}):
            result = app._guanying_login({})
        self.assertTrue(result['authenticated'])
        self.assertEqual(result['recovery_retry_seconds'], 0)
        self.assertNotIn('obsolete', app.GUANYING_LOGIN_ATTEMPTS)

    def test_clear_session_invalidates_pending_captcha(self):
        app.GUANYING_LOGIN_ATTEMPTS['obsolete'] = {'created': time.time()}
        with patch.object(app, 'require_admin'):
            app.guanying_clear_session('test')
        with self.assertRaises(app.HTTPException) as caught:
            app._guanying_captcha_verify({'attempt_id': 'obsolete'})
        self.assertEqual(caught.exception.status_code, 410)
        self.assertFalse(app.guanying_public_status()['configured'])

    def test_search_saves_browser_cookies_even_when_later_request_fails(self):
        def operation(client):
            client.session.cookies.set('verification', 'fresh', domain='www.xn--wcv59z.com')
            raise requests.Timeout('resource timeout')
        with self.assertRaises(app.HTTPException):
            self.run_operation(operation)
        restored = app.guanying_client()
        self.assertEqual(restored.session.cookies.get('verification'), 'fresh')

    def test_cooldown_expiry_allows_next_operation(self):
        with app.db() as connection:
            app.set_setting(connection, 'guanying_recovery_after', time.time() - 1)
        self.assertEqual(self.run_operation(lambda client: ['resource']), ['resource'])
        self.assertEqual(app.guanying_public_status()['recovery_retry_seconds'], 0)

    def test_rate_limit_cools_down_without_logging_in(self):
        with patch.object(self.client, 'login') as login:
            with self.assertRaises(app.HTTPException) as caught:
                self.run_operation(Mock(side_effect=GuanyingError('rate limited', status=429)))
        self.assertEqual(caught.exception.status_code, 429)
        login.assert_not_called()
        self.assertGreater(app.guanying_public_status()['recovery_retry_seconds'], 0)

    def test_manual_captcha_flow_clears_pause_and_saves_session(self):
        class Request:
            def __init__(self, payload): self.payload = payload
            async def json(self): return self.payload
        with patch.object(app, 'require_admin'), patch.object(GuanyingClient, 'login', side_effect=[{'authenticated': False}, {'authenticated': True}]), patch.object(GuanyingClient, 'captcha', return_value=None) as captcha:
            from guanying_client import CaptchaChallenge
            captcha.return_value = CaptchaChallenge(image='aW1hZ2U=', text='文字')
            login_result = asyncio.run(app.guanying_login(Request({}), 'test'))
            self.assertTrue(login_result['captcha_required'])
            with patch.object(GuanyingClient, 'verify_captcha', return_value='1,1;350;200'):
                result = asyncio.run(app.guanying_captcha_verify(Request({'attempt_id': login_result['attempt_id'], 'points': [{'x': 1, 'y': 1}]}), 'test'))
        self.assertTrue(result['authenticated'])
        self.assertEqual(result['recovery_retry_seconds'], 0)
