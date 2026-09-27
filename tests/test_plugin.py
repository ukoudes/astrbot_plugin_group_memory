"""Offline integration tests: real SQLite; AstrBot APIs are explicit test doubles."""
import asyncio
import importlib
import inspect
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    sys.modules[name] = result
    return result


def decorator(*args, **kwargs):
    return lambda fn: fn


def llm_tool(*args, **kwargs):
    def check(fn):
        params = list(inspect.signature(fn).parameters)[2:]
        documented = re.findall(r'^\s+(\w+)\((\w+)\):', inspect.getdoc(fn), re.M)
        assert params == [p for p, _ in documented], (params, documented)
        assert all(t == 'string' for _, t in documented)
        return fn
    return check


class Star:
    def __init__(self, context):
        self.context = context


class MessageChain:
    def message(self, text):
        self.text = text
        return self


module('astrbot')
module('astrbot.api', AstrBotConfig=dict, logger=logging.getLogger('test'))
module('astrbot.api.event', AstrMessageEvent=object, MessageChain=MessageChain,
       filter=types.SimpleNamespace(
    event_message_type=decorator, on_llm_request=decorator,
    on_astrbot_loaded=decorator, command=decorator,
    llm_tool=llm_tool, EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1)))
module('astrbot.api.star', Context=object, Star=Star, register=decorator)
module('astrbot.core')
module('astrbot.core.utils')
module('astrbot.core.utils.astrbot_path', get_astrbot_data_path=lambda: '.')
main = importlib.import_module(ROOT.name + '.main')
core = importlib.import_module(ROOT.name + '.core')
store = importlib.import_module(ROOT.name + '.store')


class Plain:
    def __init__(self, text):
        self.text = text


class Event:
    def __init__(self, group='111', user='42', mid='1', text='讨论内容',
                 timestamp=None, platform='qq-main', bot='999', admin=False,
                 adapter='aiocqhttp', post_type='message', raw_message=None):
        self.group, self.user, self.platform, self.bot = group, user, platform, bot
        self.admin, self.adapter = admin, adapter
        self.unified_msg_origin = (f'{adapter}:GroupMessage:{group}' if group else
                                   f'{adapter}:FriendMessage:{user}')
        self.message_str = text
        raw = {'post_type': post_type, 'message_type': 'group',
               'message': [{'type': 'text', 'data': {'text': text}}],
               'raw_message': text} if raw_message is None else raw_message
        self.message_obj = types.SimpleNamespace(
            message_id=mid, timestamp=timestamp or time.time() - 10, raw_message=raw)
        self.chain = [Plain(text)]
        self.stopped = False
    def get_platform_name(self): return self.adapter
    def get_platform_id(self): return self.platform
    def get_self_id(self): return self.bot
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.user
    def get_sender_name(self): return '群友'
    def get_messages(self): return self.chain
    def is_admin(self): return self.admin
    def plain_result(self, text): return text
    def stop_event(self): self.stopped = True


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.get_astrbot_data_path = lambda: self.tmp.name
        self.config = dict(record_groups=['111', '222'], reader_qq_ids=['42'], retention_days=30)
        self.plugin = main.GroupMemory(None, self.config)
        self.event = Event()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def read(self, event=None, **kwargs):
        return json.loads(await self.plugin.history_read(event or self.event, period='最近3小时', **kwargs))

    async def test_unmentioned_record_and_dedup(self):
        await self.plugin.record(self.event)
        await self.plugin.record(self.event)
        self.assertEqual((await self.read())['total_matching'], 1)

    async def test_actual_read_span_is_distinct_from_query_window(self):
        first_time = time.time() - 120
        last_time = time.time() - 60
        await self.plugin.record(Event(mid='early', timestamp=first_time))
        await self.plugin.record(Event(mid='late', timestamp=last_time))
        result = await self.read()
        self.assertEqual(result['read_count_so_far'], 2)
        self.assertEqual(result['read_first_time'], core.display_time(first_time))
        self.assertEqual(result['read_last_time'], core.display_time(last_time))
        self.assertNotEqual(result['start'], result['read_first_time'])
        empty = await self.read(keyword='不存在的词')
        self.assertEqual(empty['read_count_so_far'], 0)
        self.assertEqual(empty['read_first_time'], '')
        self.assertEqual(empty['read_last_time'], '')

    async def test_media_hint_uses_saved_segments_not_plain_words(self):
        await self.plugin.record(Event(mid='plain', text='这张图片应该稍后发'))
        image = Event(mid='image', text='', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'image', 'data': {'file': 'qq-image-ref'}}]})
        image.chain = []
        await self.plugin.record(image)
        result = await self.read()
        by_id = {item['message_id']: item for item in result['messages']}
        self.assertNotIn('media_read_hint', by_id['plain'])
        self.assertEqual(by_id['image']['media_types'], ['image'])
        self.assertTrue(by_id['image']['media_read_hint'])

    async def test_reader_and_groupwide_mentions_are_scoped(self):
        await self.plugin.record(Event(mid='direct', text='请提交材料', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'at', 'data': {'qq': '42'}},
                {'type': 'text', 'data': {'text': '请提交材料'}}]}))
        await self.plugin.record(Event(mid='all', text='报名今天截止', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'at', 'data': {'qq': 'all'}},
                {'type': 'text', 'data': {'text': '报名今天截止'}}]}))
        await self.plugin.record(Event(mid='other', text='旁边的账号', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'at', 'data': {'qq': '420'}},
                {'type': 'text', 'data': {'text': '旁边的账号'}}]}))
        await self.plugin.record(Event(mid='legacy', text='@42 请看一下'))
        by_id = {item['message_id']: item for item in (await self.read())['messages']}
        self.assertTrue(by_id['direct']['reader_mentioned'])
        self.assertTrue(by_id['all']['groupwide_mentioned'])
        self.assertNotIn('reader_mentioned', by_id['all'])
        self.assertNotIn('reader_mentioned', by_id['other'])
        self.assertTrue(by_id['legacy']['reader_mention_text_hint'])
        self.assertNotIn('reader_mentioned', by_id['legacy'])
        self.config['reader_qq_ids'].append('43')
        other_reader = {item['message_id']: item for item in
                        (await self.read(Event(user='43')))['messages']}
        self.assertNotIn('reader_mentioned', other_reader['direct'])

    async def test_notices_and_requests_are_not_recorded(self):
        await self.plugin.record(Event(mid='notice', text='', post_type='notice'))
        await self.plugin.record(Event(mid='request', text='null', post_type='request'))
        self.assertEqual((await self.read())['total_matching'], 0)

    async def test_raw_onebot_text_fallback(self):
        event = Event(mid='raw', text='')
        event.chain = []
        event.message_str = ''
        event.message_obj.raw_message = {
            'post_type': 'message',
            'message': [{'type': 'text', 'data': {'text': '从NapCat原始消息恢复'}}],
            'raw_message': '从NapCat原始消息恢复',
        }
        await self.plugin.record(event)
        result = await self.read()
        self.assertEqual(result['messages'][0]['content'], '从NapCat原始消息恢复')

    async def test_raw_onebot_non_text_segments(self):
        cases = [
            ('mface', {'summary': '[拜托]'}, '[商城表情:拜托]'),
            ('face', {'id': '43'}, '[QQ表情:43]'),
            ('dice', {'result': '6'}, '[骰子:6]'),
            ('rps', {'result': '1'}, '[猜拳:1]'),
            ('poke', {'id': '1'}, '[戳一戳]'),
            ('json', {'data': '{}'}, '[JSON卡片]'),
            ('new_segment', {}, '[OneBot消息段:new_segment]'),
        ]
        for index, (kind, data, expected) in enumerate(cases):
            event = Event(mid='segment-' + str(index), text='')
            event.chain = []
            event.message_str = ''
            event.message_obj.raw_message = {
                'post_type': 'message',
                'message': [{'type': kind, 'data': data}],
                'raw_message': '',
            }
            await self.plugin.record(event)
            result = await self.read(keyword=expected)
            self.assertEqual(result['total_matching'], 1, kind)

    async def test_reply_context_is_resolved_locally(self):
        await self.plugin.record(Event(mid='original', text='被引用的原文'))
        reply = Event(mid='reply', text='')
        reply.chain = [
            type('Reply', (), {'id': 'original'})(),
            Plain('这是对原文的回复'),
        ]
        await self.plugin.record(reply)
        result = await self.read(keyword='这是对原文的回复')
        contexts = result['messages'][0]['reply_contexts']
        self.assertEqual(contexts[0]['message_id'], 'original')
        self.assertEqual(contexts[0]['content'], '被引用的原文')

    async def test_reply_context_is_not_repeated_when_original_is_on_page(self):
        await self.plugin.record(Event(mid='original', text='被引用的原文'))
        reply = Event(mid='reply', text='')
        reply.chain = [type('Reply', (), {'id': 'original'})(), Plain('回复正文')]
        await self.plugin.record(reply)
        result = await self.read()
        self.assertEqual({item['message_id'] for item in result['messages']}, {'original', 'reply'})
        self.assertNotIn('reply_contexts', next(item for item in result['messages']
                                                if item['message_id'] == 'reply'))

    async def test_missing_reply_context_is_not_invented(self):
        reply = Event(mid='reply', text='')
        reply.chain = [type('Reply', (), {'id': 'not-saved'})(), Plain('回复')]
        await self.plugin.record(reply)
        result = await self.read()
        self.assertNotIn('reply_contexts', result['messages'][0])

    async def test_since_last_summary_is_per_reader(self):
        await self.plugin.record(Event(mid='first', timestamp=time.time() - 20))
        first = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(first['total_matching'], 1)
        self.assertFalse(first['previous_checkpoint_found'])
        self.assertTrue(first['checkpoint_updated'])

        empty = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(empty['total_matching'], 0)
        self.assertTrue(empty['previous_checkpoint_found'])

        await asyncio.sleep(0.01)
        await self.plugin.record(Event(mid='second', timestamp=time.time()))
        latest = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual([item['message_id'] for item in latest['messages']], ['second'])

        self.config['reader_qq_ids'].append('43')
        other = Event(user='43')
        other_result = json.loads(await self.plugin.history_read(
            other, group_id='111', period='上次总结后'))
        self.assertEqual(other_result['total_matching'], 2)

    async def test_incremental_empty_fast_path_keeps_checkpoint(self):
        first = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(first['total_matching'], 0)
        self.assertFalse(first['no_new_messages'])
        self.assertFalse(first['previous_checkpoint_found'])
        self.assertFalse(first['checkpoint_updated'])
        await self.plugin.record(Event(mid='arrived', timestamp=time.time() - 5))
        read = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(read['total_matching'], 1)
        self.assertTrue(read['checkpoint_updated'])
        db = await self.plugin.database()
        checkpoint = db.get_checkpoint('qq-main', '999', '111', '42')
        empty = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertTrue(empty['no_new_messages'])
        self.assertEqual(empty['messages'], [])
        self.assertFalse(empty['checkpoint_updated'])
        self.assertEqual(db.get_checkpoint('qq-main', '999', '111', '42'), checkpoint)
        await asyncio.sleep(0.01)
        await self.plugin.record(Event(mid='late-arrival', timestamp=checkpoint + 0.001))
        late = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual([item['message_id'] for item in late['messages']], ['late-arrival'])
        self.assertTrue(self.plugin.timings['记录库查询'])
        self.assertTrue(self.plugin.timings['记录读取工具'])

    async def test_since_last_summary_checkpoint_waits_for_last_page(self):
        ts = time.time() - 120
        for i in range(205):
            await self.plugin.record(Event(mid=str(i), timestamp=ts))
        page = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertTrue(page['has_more'])
        self.assertFalse(page['checkpoint_updated'])
        while page['has_more']:
            page = json.loads(await self.plugin.history_read(
                self.event, group_id='111', period='上次总结后',
                cursor=page['next_cursor']))
        self.assertTrue(page['checkpoint_updated'])
        after = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(after['total_matching'], 0)

    async def test_webchat_is_opt_in_and_reader_allowlisted(self):
        await self.plugin.record(Event(mid='qq-message'))
        webchat = Event(user='browser-user', adapter='webchat', platform='webchat', bot='webchat')
        denied = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='最近3小时'))
        self.assertIn('error', denied)

        self.config['enable_webchat'] = True
        still_denied = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='最近3小时'))
        self.assertIn('error', still_denied)

        self.config['webchat_reader_ids'] = ['browser-user']
        result = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='最近3小时'))
        self.assertEqual(result['total_matching'], 1)
        self.assertEqual(result['messages'][0]['message_id'], 'qq-message')
        listing = json.loads(await self.plugin.history_groups(webchat))
        self.assertEqual(listing['source'], {'platform_id': 'qq-main', 'bot_id': '999'})

    async def test_webchat_admin_requires_feature_enabled(self):
        await self.plugin.record(Event())
        admin = Event(user='web-admin', adapter='webchat', platform='webchat',
                      bot='webchat', admin=True)
        self.assertIn('error', json.loads(await self.plugin.history_read(
            admin, group_id='111', period='最近3小时')))
        self.config['enable_webchat'] = True
        result = json.loads(await self.plugin.history_read(
            admin, group_id='111', period='最近3小时'))
        self.assertEqual(result['total_matching'], 1)

    async def test_webchat_multiple_sources_must_be_selected(self):
        await self.plugin.record(Event(mid='main'))
        await self.plugin.record(Event(mid='other', platform='qq-backup', bot='888'))
        self.config.update(enable_webchat=True, webchat_reader_ids=['browser-user'])
        webchat = Event(user='browser-user', adapter='webchat', platform='webchat', bot='webchat')
        ambiguous = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='最近3小时'))
        self.assertIn('多个QQ数据源', ambiguous['error'])

        self.config['webchat_qq_platform_id'] = 'qq-main'
        self.config['webchat_qq_bot_id'] = '999'
        selected = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='最近3小时'))
        self.assertEqual([item['message_id'] for item in selected['messages']], ['main'])

    async def test_webchat_checkpoint_is_separate_from_qq(self):
        await self.plugin.record(Event(mid='first', timestamp=time.time() - 20))
        qq = json.loads(await self.plugin.history_read(
            self.event, group_id='111', period='上次总结后'))
        self.assertEqual(qq['total_matching'], 1)
        self.config.update(enable_webchat=True, webchat_reader_ids=['42'])
        webchat = Event(user='42', adapter='webchat', platform='webchat', bot='webchat')
        browser = json.loads(await self.plugin.history_read(
            webchat, group_id='111', period='上次总结后'))
        self.assertEqual(browser['total_matching'], 1)
        self.assertFalse(browser['previous_checkpoint_found'])

    async def test_scope_and_identity(self):
        for event in [Event(), Event(group='222'), Event(platform='other'), Event(bot='888')]:
            await self.plugin.record(event)
        self.assertEqual((await self.read())['total_matching'], 1)
        self.assertIn('error', await self.read(Event(user='intruder')))
        self.assertIn('error', await self.read(group_id='222'))
        self.assertIn('error', await self.read(Event(group='')))
        self.assertEqual((await self.read(Event(group=''), group_id='222'))['total_matching'], 1)
        self.assertEqual((await self.read(Event(user='admin', admin=True)))['total_matching'], 1)
        self.assertIn('error', await self.read(Event(adapter='webchat', admin=True)))
        listing = json.loads(await self.plugin.history_groups(self.event))
        self.assertEqual([g['group_id'] for g in listing['groups']], ['111'])

    async def test_health_is_private_and_does_not_call_model(self):
        calls = []
        class Client:
            async def call_action(self, name, **kwargs):
                calls.append((name, kwargs))
                return {'status': 'ok', 'retcode': 0, 'data': {'user_id': 999}}
        class Platform:
            def get_client(self): return Client()
        class Context:
            def get_platform_inst(self, platform):
                self_platform = platform
                assert self_platform == 'qq-main'
                return Platform()
            async def llm_generate(self, **kwargs):
                raise AssertionError('自检不得调用视觉模型')
        self.plugin.context = Context()
        self.config['media_vision_provider'] = 'vision'
        await self.plugin.record(Event(mid='saved', text='秘密正文'))
        await self.read()
        self.plugin.media_failure_counts['图片地址超时'] = 2
        private = Event(group='')
        replies = [item async for item in self.plugin.health(private)]
        self.assertEqual(len(replies), 1)
        self.assertIn('群 111：1 条', replies[0])
        self.assertIn('群 222：暂无已保存记录', replies[0])
        self.assertIn('QQ API：可调用', replies[0])
        self.assertIn('图片地址超时 2 次', replies[0])
        self.assertNotIn('秘密正文', replies[0])
        self.assertIn('近期耗时（每类最多保留 50 次', replies[0])
        self.assertIn('记录库查询：', replies[0])
        self.assertEqual(calls, [('get_login_info', {'self_id': '999'})])
        self.assertEqual([item async for item in self.plugin.health(Event(group='111'))], [])
        self.assertEqual(len(calls), 1)

    async def test_health_denied_and_webchat_no_source(self):
        denied = [item async for item in self.plugin.health(Event(group='', user='intruder'))]
        self.assertIn('没有查询权限', denied[0])
        self.assertIsNone(self.plugin.archive)
        self.config.update(enable_webchat=True, webchat_reader_ids=['browser-user'])
        webchat = Event(group='', user='browser-user', adapter='webchat',
                        platform='webchat', bot='webchat')
        replies = [item async for item in self.plugin.health(webchat)]
        self.assertIn('记录库：可用', replies[0])
        self.assertIn('尚未发现可用的NapCat QQ记录数据源', replies[0])
        self.assertIn('QQ API：未检查', replies[0])
        self.config.update(webchat_qq_platform_id='qq-main', webchat_qq_bot_id='999')
        selected = [item async for item in self.plugin.health(webchat)]
        self.assertIn('群 111：暂无已保存记录', selected[0])
        self.assertIn('QQ API：AstrBot 当前未找到此 QQ 接入的 API 客户端', selected[0])

    async def test_health_distinguishes_api_failure_and_database_failure(self):
        class Client:
            async def call_action(self, name, **kwargs):
                raise RuntimeError('secret-url-and-token')
        class Context:
            def get_platform_inst(self, platform):
                return types.SimpleNamespace(get_client=lambda: Client())
        self.plugin.context = Context()
        reply = [item async for item in self.plugin.health(Event(group=''))]
        self.assertIn('记录库：可用', reply[0])
        self.assertIn('QQ API：调用失败（RuntimeError）', reply[0])
        self.assertNotIn('secret-url-and-token', reply[0])
        async def broken_database():
            raise RuntimeError('secret-database-path')
        self.plugin.database = broken_database
        reply = [item async for item in self.plugin.health(Event(group=''))]
        self.assertIn('记录库：不可用', reply[0])
        self.assertIn('QQ API：未检查', reply[0])
        self.assertNotIn('secret-database-path', reply[0])

    async def test_whitelist_bot_and_other_platform_not_recorded(self):
        for event in [Event(group='333'), Event(user='999'), Event(adapter='telegram')]:
            await self.plugin.record(event)
        self.assertEqual((await self.read())['total_matching'], 0)
        self.config['record_groups'] = []
        await self.plugin.record(Event())
        self.assertIn('error', await self.read())

    async def test_pagination_no_loss_same_timestamp_and_new_arrival(self):
        ts = time.time() - 120
        for i in range(205):
            await self.plugin.record(Event(mid=str(i), timestamp=ts))
        first = await self.read()
        self.assertTrue(first['has_more'])
        await self.plugin.record(Event(mid='new', timestamp=ts))
        ids = [m['message_id'] for m in first['messages']]
        page = first
        while page['has_more']:
            page = await self.read(cursor=page['next_cursor'])
            ids.extend(m['message_id'] for m in page['messages'])
        self.assertEqual(len(ids), 205)
        self.assertEqual(page['read_count_so_far'], 205)
        self.assertEqual(page['read_first_time'], core.display_time(ts))
        self.assertEqual(page['read_last_time'], core.display_time(ts))
        self.assertEqual(len(set(ids)), 205)
        self.assertNotIn('new', ids)
        self.assertEqual((await self.read())['total_matching'], 206)
        self.assertIn('error', await self.read(cursor=first['next_cursor'][:-3] + 'xxx'))
        self.assertIn('error', await self.read(cursor=first['next_cursor'], keyword='讨论'))
        self.config['reader_qq_ids'] = []
        self.assertIn('error', await self.read(cursor=first['next_cursor']))

    async def test_long_messages_bounded_and_media(self):
        for i in range(12):
            await self.plugin.record(Event(mid=str(i), text='长' * 6000))
        page = await self.read()
        self.assertTrue(page['has_more'])
        self.assertLess(len(core.dumps(page)), 19000)
        self.assertTrue(all(m['truncated'] for m in page['messages']))
        media = Event(mid='media')
        media.chain = [type('Image', (), {})(), type('Reply', (), {'id': '321'})()]
        await self.plugin.record(media)
        result = await self.read(keyword='引用消息ID')
        self.assertIn('[图片，未解析内容]', result['messages'][0]['content'])
        self.assertIn('321', result['messages'][0]['content'])

    async def test_sql_input_is_literal_and_sender_filter(self):
        await self.plugin.record(Event(text="链接100%_ ' OR 1=1 --"))
        await self.plugin.record(Event(mid='2', user='43', text='其他内容'))
        self.assertEqual((await self.read(keyword="' OR 1=1 --"))['total_matching'], 1)
        self.assertEqual((await self.read(keyword='%_'))['total_matching'], 1)
        self.assertEqual((await self.read(sender_id='43'))['total_matching'], 1)
        self.assertEqual((await self.read(keyword='missing'))['total_matching'], 0)

    async def test_restart_persistence_and_expiry(self):
        await self.plugin.record(Event())
        second = main.GroupMemory(None, self.config)
        result = json.loads(await second.history_read(self.event, period='最近3小时'))
        self.assertEqual(result['total_matching'], 1)
        db = await self.plugin.database()
        db.insert(dict(platform='qq-main', bot='999', group_id='111', message_id='old',
                       timestamp=time.time() - 40 * 86400, sender_id='42', sender_name='旧',
                       content='过期', truncated=0))
        self.plugin.last_cleanup = 0
        await self.plugin.database()
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)

    async def test_concurrent_recording(self):
        await asyncio.gather(*(self.plugin.record(Event(mid=str(i))) for i in range(40)))
        self.assertEqual((await self.read())['total_matching'], 40)

    async def test_guidance_and_invalid_time(self):
        request = types.SimpleNamespace(system_prompt='原有人格')
        await self.plugin.add_guidance(self.event, request)
        self.assertTrue(request.system_prompt.startswith('原有人格'))
        self.assertIn(main.GUIDANCE, request.system_prompt)
        self.assertIn('你的待办与提醒', request.system_prompt)
        self.assertIn('加粗标题单独一行', request.system_prompt)
        self.assertIn('默认读取查询时段内全部', request.system_prompt)
        result = await self.read()
        self.assertNotIn('instruction', result)
        self.config['summary_media_scope'] = '重点媒体'
        request = types.SimpleNamespace(system_prompt='原有人格')
        await self.plugin.add_guidance(self.event, request)
        self.assertIn('媒体是选择性读取', request.system_prompt)
        denied_request = types.SimpleNamespace(system_prompt='原有人格')
        await self.plugin.add_guidance(Event(user='intruder'), denied_request)
        self.assertEqual(denied_request.system_prompt, '原有人格')
        self.assertIn('error', await self.read(start='bad', end='worse'))
        self.assertIn('error', await self.read(start='2026-01-01', end='2026-03-01'))


class TimeTests(unittest.TestCase):
    def test_beijing_dates_on_utc_host(self):
        now = core.datetime(2026, 9, 21, 1, 0, tzinfo=core.TZ).timestamp()
        start, end = core.time_range('昨天', '', '', now)
        self.assertEqual(core.display_time(start), '2026-09-20T00:00:00+08:00')
        self.assertEqual(end - start, 86400)
        a, b = core.time_range('', '2026-09-20T16:00:00Z', '2026-09-21T01:00:00+08:00', now)
        self.assertEqual(b - a, 3600)

    def test_reject_zero_and_unbounded(self):
        for period in ['最近0小时', '最近32天', 'abc']:
            with self.assertRaises(ValueError):
                core.time_range(period, '', '', time.time())


if __name__ == '__main__':
    unittest.main()
