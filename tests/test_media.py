"""Read-only fake OneBot/vision integration; no live QQ or model requests."""
import asyncio
import json
import types
import time
from unittest import mock
import test_plugin as base
from test_plugin import Event, main, store
from contextlib import closing
from astrbot_plugin_group_memory.media import Reader, sanitize, image_url
import unittest


class Client:
    def __init__(self):
        self.calls = []
        self.responses = {'get_login_info': {'user_id': 999}}
    async def call_action(self, name, **kwargs):
        self.calls.append((name, kwargs))
        result = self.responses[name]
        action_args = {key: value for key, value in kwargs.items() if key != 'self_id'}
        return result(**action_args) if callable(result) else result


class Context:
    def __init__(self):
        self.client = Client()
        self.vision_calls = []
    def get_platform_inst(self, pid):
        assert pid == 'qq-main'
        return types.SimpleNamespace(get_client=lambda: self.client)
    async def llm_generate(self, **kwargs):
        self.vision_calls.append(kwargs)
        return types.SimpleNamespace(completion_text='截图文字：报名周五截止')


# Reuse setup helpers, not the complete parent test suite.
class MediaTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.PluginTests.asyncSetUp
    asyncTearDown = base.PluginTests.asyncTearDown

    async def save(self, segments, mid='100'):
        e = Event(mid=mid, text='', raw_message={'post_type':'message', 'message':segments})
        e.chain = []
        await self.plugin.record(e)
        return e

    async def media(self, mid='100', event=None, **kwargs):
        return json.loads(await self.plugin.history_media(event or self.event, group_id='111', message_id=mid, **kwargs))

    async def test_image_cache_restart_and_expiry(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}])
        result = await self.media()
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'][0]['type'], 'image_interpretation')
        self.assertTrue(self.plugin.timings['单条媒体工具'])
        self.assertTrue(self.plugin.timings['QQ get_login_info'])
        self.assertTrue(self.plugin.timings['识图模型'])
        self.assertEqual(ctx.vision_calls[0]['image_urls'], ['https://gchat.qpic.cn/test'])
        self.plugin = main.GroupMemory(ctx, self.config)
        self.assertTrue((await self.media())['cached'])
        self.assertEqual(len(ctx.vision_calls), 1)
        await self.media(refresh='true')
        self.assertEqual(len(ctx.vision_calls), 2)
        db = await self.plugin.database()
        db.purge(time.time() + 1)
        self.assertEqual(db.media_data(('qq-main','999','111','100'), 'vision'), (None,None))

    async def test_unauthorized_and_disabled_make_no_calls(self):
        ctx = self.plugin.context = Context()
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}])
        self.assertIn('error', await self.media(event=Event(user='intruder')))
        self.assertIn('error', await self.media(mid='not-saved'))
        self.config['enable_media_read'] = False
        self.assertIn('error', await self.media())
        self.assertEqual(ctx.client.calls, [])

    async def test_nested_forward_and_reply_group_check(self):
        ctx = self.plugin.context = Context()
        ctx.client.responses['get_forward_msg'] = {'messages':[
            {'sender':{'nickname':'甲'},'content':[{'type':'text','data':{'text':'资源介绍'}},
               {'type':'forward','data':{'id':'nested'}}]}]}
        def forward(message_id):
            if message_id == 'nested':
                return {'messages':[{'sender':{},'content':[{'type':'text','data':{'text':'嵌套正文'}}]}]}
            return {'messages':[{'sender':{},'content':[{'type':'forward','data':{'id':'nested'}}]}]}
        ctx.client.responses['get_forward_msg'] = forward
        ctx.client.responses['get_msg'] = {'group_id':222,'message_type':'group','message':[{'type':'text','data':{'text':'SECRET'}}]}
        await self.save([{'type':'forward','data':{'id':'root'}},{'type':'reply','data':{'id':'456'}}])
        result = await self.media()
        self.assertIn('嵌套正文', json.dumps(result,ensure_ascii=False))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertFalse(result['complete'])
        self.assertTrue(all(name in ('get_login_info','get_forward_msg','get_msg') for name,_ in ctx.client.calls))

    async def test_missing_provider_and_unsupported_are_explicit(self):
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}},
                         {'type':'record','data':{'file':'abc'}}, {'type':'video','data':{}}, {'type':'file','data':{}}])
        result = await self.media()
        self.assertFalse(result['complete'])
        self.assertEqual({i['type'] for i in result['issues']}, {'image','record','video','file'})
        self.assertFalse(result['cached'])
        self.assertEqual(self.plugin.media_failure_counts['未配置识图模型'], 1)
        self.assertEqual(self.plugin.media_failure_counts['暂不支持的媒体格式'], 3)

    async def test_old_message_fallback_and_wrong_bot(self):
        ctx = self.plugin.context = Context()
        await self.save([{'type':'text','data':{'text':'old'}}])
        db = await self.plugin.database()
        with closing(db.connect()) as conn, conn:
            conn.execute('DELETE FROM media_payloads')
        ctx.client.responses['get_msg'] = {'message_type':'group','group_id':111,
                                          'message':[{'type':'text','data':{'text':'restored'}}]}
        self.assertEqual((await self.media())['items'][0]['content'], 'restored')
        ctx.client.responses['get_login_info'] = {'user_id':888}
        self.assertIn('error', await self.media(refresh='true'))

    async def test_local_reply_works_offline(self):
        await self.save([{'type':'text','data':{'text':'本地原文'}}], mid='101')
        await self.save([{'type':'reply','data':{'id':'101'}}])
        result = await self.media()
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'][0]['content'], '本地原文')

    async def test_provider_errors_do_not_leak(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        async def fail(**kwargs):
            raise ValueError('secret-token-private-url')
        ctx.llm_generate = fail
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}])
        result = await self.media()
        self.assertFalse(result['complete'])
        self.assertNotIn('secret-token', json.dumps(result))

    async def test_webchat_uses_selected_qq_source(self):
        self.plugin.context = Context()
        self.config.update(enable_webchat=True, webchat_reader_ids=['browser'])
        await self.save([{'type':'text','data':{'text':'正文'}}])
        e = Event(user='browser', adapter='webchat', platform='webchat', bot='webchat', group='')
        self.assertTrue((await self.media(event=e))['complete'])

    async def test_limits_cycles_and_url_validation(self):
        ctx = Context()
        r = Reader(ctx, ctx.client, '111', 'vision')
        await r.expand(sanitize([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}]*6))
        self.assertEqual(len(ctx.vision_calls), 4)
        self.assertFalse(r.result()['complete'])
        self.assertEqual(
            image_url('https://multimedia.nt.qq.com.cn/download?rkey=test'),
            'https://multimedia.nt.qq.com.cn/download?rkey=test',
        )
        self.assertEqual(
            image_url('http://multimedia.nt.qq.com.cn/download?rkey=test'),
            'https://multimedia.nt.qq.com.cn/download?rkey=test',
        )
        for url in [
            'file:///etc/passwd',
            'http://127.0.0.1/image',
            'https://gchat.qpic.cn.evil.com/x',
            'https://multimedia.nt.qq.com.cn.evil.com/x',
            'https://multimedia.nt.qq.com.cn@evil.com/x',
            'https://user:pass@gchat.qpic.cn/x',
        ]:
            self.assertIsNone(image_url(url))
        self.assertEqual(sanitize([{'type':'text','data':{'text':'x'*5000}}])[-1]['type'], 'unavailable')
        ctx.client.responses['get_forward_msg'] = {'messages':[{'content':[{'type':'forward','data':{'id':'loop'}}]}]}
        r = Reader(ctx, ctx.client, '111', '')
        await r.expand([{'type':'forward','data':{'id':'loop'}}])
        self.assertEqual(r.calls,1)
        self.assertTrue(any(i['type']=='duplicate_reference' for i in r.items))

    async def test_existing_database_upgrade_preserves_records(self):
        await self.save([{'type':'text','data':{'text':'已有文字'}}])
        db = await self.plugin.database()
        with closing(db.connect()) as conn, conn:
            conn.execute('DROP TABLE media_payloads')
            conn.execute('DROP TABLE media_cache')
        upgraded = store.Archive(db.path)
        rows = upgraded.resolve_messages('qq-main','999','111',['100'])
        self.assertEqual(rows['100']['content'], '已有文字')
        self.assertEqual(upgraded.media_data(('qq-main','999','111','100'), ''), (None,None))

    async def test_timeout_is_partial_and_retryable(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        async def timeout(**kwargs):
            raise asyncio.TimeoutError()
        ctx.llm_generate = timeout
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}])
        result = await self.media()
        self.assertFalse(result['complete'])
        db = await self.plugin.database()
        self.assertIsNone(db.media_data(('qq-main','999','111','100'), 'vision')[1])
        ctx.llm_generate = Context.llm_generate.__get__(ctx)
        self.assertTrue((await self.media())['complete'])

    async def test_get_image_fallback_and_response_envelope(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        ctx.client.responses['get_image'] = {
            'status':'ok', 'retcode':0,
            'data':{'url':'http://multimedia.nt.qq.com.cn/download?appid=1407&fileid=test'},
        }
        await self.save([{'type':'image','data':{'file':'hash.image'}}])
        self.assertTrue((await self.media())['complete'])
        self.assertEqual(ctx.vision_calls[0]['image_urls'], [
            'https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=test'])
        result = await self.media()
        self.assertEqual(result['diagnostics'][0], {
            'segment_type': 'image',
            'direct_url': False,
            'file_reference': True,
            'address_source': 'get_image',
            'status': 'recognized',
        })

    async def test_automatic_reader_prefers_fresh_qq_image_address(self):
        ctx = Context()
        ctx.client.responses['get_image'] = {'url': 'https://gchat.qpic.cn/fresh'}
        reader = Reader(ctx, ctx.client, '111', 'vision', bot_id='999',
                        prefer_fresh_image_url=True)
        await reader.expand([{'type': 'image', 'data': {
            'url': 'https://gchat.qpic.cn/expired', 'file': 'image-id'}}])
        self.assertEqual(ctx.vision_calls[0]['image_urls'], ['https://gchat.qpic.cn/fresh'])
        self.assertEqual(reader.diagnostics[0]['address_source'], 'get_image')

    async def test_get_image_timeout_does_not_retry_even_if_next_attempt_would_succeed(self):
        ctx = Context()
        attempts = 0
        async def flaky(name, **kwargs):
            nonlocal attempts
            self.assertEqual(name, 'get_image')
            attempts += 1
            if attempts == 1:
                raise asyncio.TimeoutError()
            return {'url':'https://multimedia.nt.qq.com.cn/download?rkey=second'}
        ctx.client.call_action = flaky
        reader = Reader(
            ctx, ctx.client, '111', 'vision', bot_id='999',
            get_image_timeout=25)
        await reader.expand([{'type':'image','data':{'file':'hash.image'}}])
        result = reader.result()
        self.assertFalse(result['complete'])
        self.assertEqual(attempts, 1)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(result['diagnostics'][0]['status'], 'address_fetch_failed')

    async def test_get_image_timeout_is_diagnostic_and_not_cached(self):
        ctx = Context()
        calls = []
        async def timeout(name, **kwargs):
            calls.append((name, kwargs))
            raise asyncio.TimeoutError()
        ctx.client.call_action = timeout
        reader = Reader(
            ctx, ctx.client, '111', 'vision', bot_id='999',
            get_image_timeout=25)
        await reader.expand([{'type':'mface','data':{'file':'emoji.image'}}])
        result = reader.result()
        self.assertFalse(result['complete'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(kwargs['self_id'] == '999' for _, kwargs in calls))
        self.assertIn('单次等待25秒，不自动重试', result['issues'][0]['reason'])
        self.assertEqual(result['diagnostics'][0], {
            'segment_type': 'mface',
            'direct_url': False,
            'file_reference': True,
            'address_source': 'get_image',
            'status': 'address_fetch_failed',
            'reason': 'get_image：TimeoutError，单次等待25秒，不自动重试',
        })

    async def test_get_image_timeout_cooldown_skips_repeat_but_refresh_bypasses(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        self.config['media_get_image_retries'] = 2  # Obsolete saved config must not retry.
        attempts = 0

        def timeout(file):
            nonlocal attempts
            attempts += 1
            raise asyncio.TimeoutError()

        ctx.client.responses['get_image'] = timeout
        await self.save([{'type': 'image', 'data': {'file': 'hash.image'}}], mid='cooldown')
        first = await self.media(mid='cooldown')
        self.assertFalse(first['complete'])
        self.assertEqual(attempts, 1)
        self.assertEqual(self.plugin.media_failure_counts['图片地址超时'], 1)
        second = await self.media(mid='cooldown')
        self.assertTrue(second['retry_skipped'])
        self.assertFalse(second['complete'])
        self.assertEqual(attempts, 1)
        batch = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids='["cooldown"]'))
        self.assertTrue(batch['results'][0]['retry_skipped'])
        self.assertEqual(attempts, 1)
        self.assertEqual(self.plugin.media_failure_counts['图片地址超时'], 1)
        await self.media(mid='cooldown', refresh='true')
        self.assertEqual(attempts, 2)
        self.assertEqual(self.plugin.media_failure_counts['图片地址超时'], 2)

    async def test_batch_reads_multiple_messages_and_reports_each_image(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        await self.save([{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/one'}}], mid='101')
        await self.save([{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/two'}}], mid='102')
        await self.save([{'type': 'image', 'data': {'file': 'missing.file'}}], mid='103')
        ctx.client.responses['get_image'] = {'url': 'file:///not-allowed'}
        result = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids='["101","102","103"]'))
        self.assertEqual(result['requested_count'], 3)
        self.assertEqual(result['success_count'], 2)
        self.assertEqual(result['failure_count'], 1)
        self.assertEqual(result['image_success_count'], 2)
        self.assertEqual(result['image_failure_count'], 1)
        self.assertEqual(self.plugin.media_failure_counts['无可用图片地址'], 1)
        self.assertFalse(result['complete_coverage'])
        self.assertEqual([item['message_id'] for item in result['results']], ['101', '102', '103'])
        self.assertIn('截图文字', result['results'][0]['images'][0]['content'])
        self.assertEqual(result['results'][2]['images'][0]['status'], 'address_unavailable')
        self.assertTrue(result['results'][2]['images'][0]['reason'])
        self.assertTrue(result['original_text_omitted'])

        repeat = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids='["101","102"]'))
        self.assertTrue(all(item['cached'] for item in repeat['results']))
        self.assertEqual(len(ctx.vision_calls), 2)

    async def test_batch_omits_original_text_but_keeps_forward_text(self):
        ctx = self.plugin.context = Context()
        ctx.client.responses['get_forward_msg'] = {'messages': [
            {'sender': {'nickname': '甲'}, 'content': [
                {'type': 'text', 'data': {'text': '转发中的资源'}}]}]}
        await self.save([{'type': 'text', 'data': {'text': '原消息已在分页中'}},
                         {'type': 'forward', 'data': {'id': 'fwd'}}], mid='104')
        result = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids='["104"]'))
        body = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('原消息已在分页中', body)
        self.assertIn('转发中的资源', body)

    async def test_batch_permission_scope_and_input_validation(self):
        ctx = self.plugin.context = Context()
        await self.save([{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/one'}}], mid='101')
        for event, ids in [
            (Event(user='intruder'), '["101"]'),
            (self.event, '["101","101"]'),
            (self.event, '["101","outside"]'),
            (self.event, 'not-json'),
            (self.event, json.dumps([str(i) for i in range(9)])),
        ]:
            response = json.loads(await self.plugin.history_media_batch(
                event, group_id='111', message_ids=ids))
            self.assertIn('error', response)
        self.assertEqual(ctx.client.calls, [])
        self.assertEqual(ctx.vision_calls, [])

    async def test_batch_limits_vision_concurrency_to_four(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        active = peak = 0

        async def vision(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return types.SimpleNamespace(completion_text='可读截图文字')

        ctx.llm_generate = vision
        for index in range(8):
            await self.save([{'type': 'image', 'data': {
                'url': f'https://gchat.qpic.cn/{index}'}}], mid=str(201 + index))
        result = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids=json.dumps([str(201 + i) for i in range(8)])))
        self.assertTrue(result['complete_coverage'])
        self.assertEqual(result['image_success_count'], 8)
        self.assertEqual(peak, 4)

    async def test_batch_concurrency_can_be_reduced(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        self.config['media_batch_concurrency'] = 2
        self.plugin = main.GroupMemory(ctx, self.config)
        active = peak = 0

        async def vision(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return types.SimpleNamespace(completion_text='可读截图文字')

        ctx.llm_generate = vision
        for index in range(4):
            await self.save([{'type': 'image', 'data': {
                'url': f'https://gchat.qpic.cn/{index}'}}], mid=str(301 + index))
        result = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids=json.dumps([str(301 + i) for i in range(4)])))
        self.assertTrue(result['complete_coverage'])
        self.assertEqual(peak, 2)

    async def test_batch_timeout_marks_incomplete_without_claiming_images_read(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'

        async def slow_vision(**kwargs):
            await asyncio.sleep(0.2)
            return types.SimpleNamespace(completion_text='过时结果')

        ctx.llm_generate = slow_vision
        for index in range(3):
            await self.save([{'type': 'image', 'data': {
                'url': f'https://gchat.qpic.cn/slow-{index}'}}], mid=str(301 + index))
        with mock.patch.object(main, 'BATCH_TIMEOUT', 0.01):
            result = json.loads(await self.plugin.history_media_batch(
                self.event, group_id='111', message_ids='["301","302","303"]'))
        self.assertFalse(result['complete_coverage'])
        self.assertEqual(result['success_count'], 0)
        self.assertEqual(result['timed_out_count'], 3)
        self.assertTrue(all('error' in item for item in result['results']))

    async def test_batch_output_truncation_is_explicit(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'

        async def long_vision(**kwargs):
            return types.SimpleNamespace(completion_text='字' * 6000)

        ctx.llm_generate = long_vision
        await self.save([{'type': 'image', 'data': {
            'url': 'https://gchat.qpic.cn/long'}}], mid='401')
        result = json.loads(await self.plugin.history_media_batch(
            self.event, group_id='111', message_ids='["401"]'))
        self.assertFalse(result['complete_coverage'])
        self.assertEqual(result['success_count'], 0)
        self.assertEqual(len(result['results'][0]['images'][0]['content']), 4500)
        self.assertEqual(result['results'][0]['issues'][-1]['type'], 'batch_output')

    async def test_direct_image_url_skips_get_image_and_reports_source(self):
        ctx = Context()
        reader = Reader(ctx, ctx.client, '111', 'vision', bot_id='999')
        await reader.expand([{'type':'image','data':{
            'url':'https://multimedia.nt.qq.com.cn/download?rkey=direct',
            'file':'hash.image',
        }}])
        result = reader.result()
        self.assertTrue(result['complete'])
        self.assertEqual(ctx.client.calls, [])
        self.assertEqual(result['diagnostics'][0]['address_source'], 'message_segment')
        self.assertTrue(result['diagnostics'][0]['direct_url'])

    async def test_forward_limit_and_empty_result_are_not_complete(self):
        ctx = Context()
        ctx.client.responses['get_forward_msg'] = {'messages':[]}
        r = Reader(ctx, ctx.client, '111', '')
        await r.expand([{'type':'forward','data':{'id':'empty'}}])
        self.assertFalse(r.result()['complete'])

        ctx.client.responses['get_forward_msg'] = {'messages':[{'content':[{'type':'text','data':{'text':'node'}}]}]*40}
        r = Reader(ctx, ctx.client, '111', '')
        await r.expand([{'type':'forward','data':{'id':'many'}}])
        self.assertEqual(len(r.items),30)
        self.assertFalse(r.result()['complete'])

    async def test_background_api_routes_record_bot_explicitly(self):
        ctx = self.plugin.context = Context()
        self.config.update(enable_webchat=True, webchat_reader_ids=['browser'])
        original = ctx.client.call_action
        class ApiNotAvailable(Exception):
            pass
        async def routed(name, **kwargs):
            if kwargs.get('self_id') != '999':
                raise ApiNotAvailable()
            return await original(name, **kwargs)
        ctx.client.call_action = routed
        ctx.client.responses['get_forward_msg'] = {'messages':[{'content':[{'type':'text','data':{'text':'转发正文'}}]}]}
        await self.save([{'type':'forward','data':{'id':'fwd'}}])
        event = Event(user='browser', adapter='webchat', platform='webchat', bot='webchat', group='')
        result = await self.media(event=event)
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'][0]['content'], '转发正文')
        self.assertTrue(all(args['self_id'] == '999' for _,args in ctx.client.calls))

    async def test_disconnected_api_identifies_failing_action_without_vision_call(self):
        ctx = self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        class ApiNotAvailable(Exception):
            pass
        async def offline(name, **kwargs):
            raise ApiNotAvailable()
        ctx.client.call_action = offline
        await self.save([{'type':'image','data':{'url':'https://gchat.qpic.cn/test'}}])
        result = await self.media()
        self.assertIn('get_login_info', result['error'])
        self.assertIn('ApiNotAvailable', result['error'])
        self.assertEqual(ctx.vision_calls, [])
