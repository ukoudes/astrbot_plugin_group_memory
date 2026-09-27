"""Offline scheduled-summary tests with real SQLite and fake QQ/model delivery."""
import asyncio
import json
import sqlite3
import smtplib
import sys
import time
import types
import unittest
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path
from unittest import mock

import test_plugin as base
from test_plugin import Event, main, core
from astrbot_plugin_group_memory.auto import (checked_report, due_state, parse_time,
                                              plain_report, report_date_label,
                                              short_span, window_bounds)
from astrbot_plugin_group_memory.mailer import (email_address, render_report_html,
                                                send_email, smtp_error_detail,
                                                smtp_options)
from astrbot_plugin_group_memory.settings import public_settings, validate_settings
from astrbot_plugin_group_memory.history_backfill import fetch_history, history_message


class AutoContext:
    def __init__(self):
        self.sent = []
        self.prompts = []
        self.image_calls = 0
        self.fail_model = False
        self.provider_ids = []

    def get_platform_inst(self, platform):
        assert platform == 'qq-main'
        return types.SimpleNamespace(get_client=lambda: self)

    async def call_action(self, name, **kwargs):
        if name == 'get_group_msg_history':
            return {'status': 'ok', 'retcode': 0, 'data': {'messages': []}}
        assert name == 'get_login_info'
        assert kwargs['self_id'] == '999'
        return {'status': 'ok', 'retcode': 0, 'data': {'user_id': 999}}

    async def get_current_chat_provider_id(self, umo):
        assert umo == 'aiocqhttp:FriendMessage:42'
        return 'chat-model'

    def get_using_provider(self):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id='default-model'))

    async def llm_generate(self, **kwargs):
        self.provider_ids.append(kwargs['chat_provider_id'])
        if self.fail_model:
            raise RuntimeError('secret-model-error')
        if kwargs.get('image_urls'):
            self.image_calls += 1
            return types.SimpleNamespace(completion_text='截图中的报名信息')
        self.prompts.append(kwargs['prompt'])
        return types.SimpleNamespace(completion_text='## 重要讨论\n**报名**\n群友讨论报名。\n\n'
                                                 '## 有用资源\n无\n\n## 你的待办与提醒\n无\n\n'
                                                 '## 待确认的信息\n无')

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain.text))
        return True


class AutoTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.PluginTests.asyncSetUp
    asyncTearDown = base.PluginTests.asyncTearDown

    async def command(self, action='', target='', daily_time='', event=None):
        return [item async for item in self.plugin.auto_command(
            event or Event(group=''), action, target, daily_time)]

    async def add_due_job(self):
        now = time.time()
        daily_time = datetime.fromtimestamp(now, core.TZ).strftime('%H:%M')
        response = await self.command('添加', '111', daily_time)
        self.assertIn('已设置任务', response[0])
        return now

    async def test_private_commands_and_persistence(self):
        group_event = Event()
        self.assertEqual(await self.command('添加', '111', '20:00', group_event), [])
        self.assertTrue(group_event.stopped)
        denied = await self.command('添加', '111', '20:00', Event(group='', user='43'))
        self.assertIn('允许查询记录', denied[0])
        self.assertIn('未加入', (await self.command('添加', '333', '20:00'))[0])
        self.assertIn('时间格式', (await self.command('添加', '111', '25:00'))[0])
        self.assertIn('已设置任务', (await self.command('添加', '111', '20:00'))[0])
        self.assertIn('#1', (await self.command('列表'))[0])
        restored = main.GroupMemory(None, self.config)
        saved = [item async for item in restored.auto_command(Event(group=''), '列表')]
        self.assertIn('#1', saved[0])
        self.assertIn('已删除', (await self.command('删除', '1'))[0])
        self.assertIn('尚未设置', (await self.command('列表'))[0])

    async def test_add_starts_scheduler_and_cancel_group_removes_only_own_jobs(self):
        self.assertIsNone(self.plugin.auto_task)
        self.assertIn('不会立即总结', (await self.command('添加', '111', '20:00'))[0])
        self.assertIsNotNone(self.plugin.auto_task)
        await self.command('添加', '111', '21:00')
        await self.command('添加', '222', '20:00')
        self.assertIn('群号', (await self.command('取消', 'x'))[0])
        self.assertIn('2 个', (await self.command('取消', '111'))[0])
        listed = (await self.command('列表'))[0]
        self.assertNotIn('群 111 ', listed)
        self.assertIn('群 222 ', listed)
        self.assertIn('没有找到', (await self.command('取消', '111'))[0])
        self.assertIn('允许查询记录', (await self.command(
            '取消', '222', event=Event(group='', user='43')))[0])
        self.assertEqual(len((await self.plugin.database()).all_auto_jobs()), 1)
        await self.plugin.terminate()

    async def test_saved_job_restarts_scheduler_after_hot_reload_on_group_message(self):
        await self.command('添加', '111', '20:00')
        await self.plugin.terminate()
        restored = main.GroupMemory(None, self.config)
        self.assertIsNone(restored.auto_task)
        await restored.record(Event(mid='new-after-reload'))
        self.assertIsNotNone(restored.auto_task)
        await restored.terminate()

    async def test_web_page_manages_same_saved_jobs_as_private_commands(self):
        routes = []
        context = types.SimpleNamespace(register_web_api=lambda *args: routes.append(args))
        plugin = main.GroupMemory(context, self.config)
        self.assertEqual(len(routes), 10)
        await plugin.record(Event(mid='page-source'))
        request = types.SimpleNamespace(json=lambda **kwargs: None)
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        web.request = request
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            listed = await plugin.page_auto_jobs()
            self.assertEqual(len(listed['sources']), 1)
            async def alias_payload(**kwargs):
                return {'group_id': '111', 'display_name': '学习交流群'}
            request.json = alias_payload
            self.assertEqual((await plugin.page_auto_group_name())['display_name'], '学习交流群')
            self.assertEqual((await plugin.page_auto_jobs())['group_names']['111'], '学习交流群')
            async def add_payload(**kwargs):
                return {'group_id': '111', 'platform': 'qq-main', 'bot': '999',
                        'reader_id': '42', 'daily_time': '20:00',
                        'read_start': '08:00', 'read_end': '20:00'}
            request.json = add_payload
            created = await plugin.page_auto_add()
            self.assertEqual(created['id'], 1)
            self.assertIn('任务已保存', created['message'])
            self.assertEqual((await plugin.page_auto_jobs())['jobs'][0]['reader_id'], '42')
            self.assertEqual((await plugin.page_auto_jobs())['jobs'][0]['read_start_minute'], 480)
            archive = await plugin.database()
            archive.set_auto_job_read_stats(created['id'], 100, 200, 42, True, 201)
            shown = (await plugin.page_auto_jobs())['jobs'][0]
            self.assertEqual(shown['last_read_count'], 42)
            self.assertEqual(shown['last_read_start'], 100)
            self.assertEqual(shown['last_read_end'], 200)
            self.assertEqual(shown['last_read_partial'], 1)
            async def invalid_payload(**kwargs):
                return {**(await add_payload()), 'read_end': '21:00'}
            request.json = invalid_payload
            self.assertIn('晚于推送', (await plugin.page_auto_add())['error'])
            async def updated_payload(**kwargs):
                return {**(await add_payload()), 'read_start': '09:00'}
            request.json = updated_payload
            updated = await plugin.page_auto_add()
            self.assertEqual(updated['id'], 1)
            self.assertIn('任务已更新', updated['message'])
            self.assertEqual((await plugin.page_auto_jobs())['jobs'][0]['read_start_minute'], 540)
            today = datetime.now(core.TZ).date()
            async def fixed_payload(**kwargs):
                return {**(await add_payload()), 'read_end': '19:00',
                        'schedule_kind': 'once', 'read_date': today.isoformat(),
                        'send_date': (today + timedelta(days=1)).isoformat()}
            request.json = fixed_payload
            fixed = await plugin.page_auto_add()
            self.assertNotEqual(fixed['id'], 1)
            self.assertEqual(len((await plugin.page_auto_jobs())['jobs']), 2)
            self.assertEqual((await plugin.page_auto_jobs())['jobs'][1]['schedule_kind'],
                             'once')
            async def delete_fixed(**kwargs):
                return {'id': fixed['id']}
            request.json = delete_fixed
            self.assertTrue((await plugin.page_auto_delete())['removed'])
            await plugin.record(Event(group='222', mid='page-source-222'))
            db = await plugin.database()
            db.finish_auto_job(1, '已推送', 1, time.time())
            async def changed_window(**kwargs):
                return {**(await add_payload()), 'id': 1, 'read_start': '10:00'}
            request.json = changed_window
            self.assertEqual((await plugin.page_auto_update())['id'], 1)
            self.assertEqual(db.all_auto_jobs()[0]['cursor_id'], 1)
            self.assertEqual(db.all_auto_jobs()[0]['read_start_minute'], 600)
            async def earlier_time_without_window(**kwargs):
                return {**(await add_payload()), 'id': 1, 'daily_time': '19:00'}
            request.json = earlier_time_without_window
            self.assertIn('结束时间不能晚于推送时间',
                          (await plugin.page_auto_update())['error'])
            self.assertEqual(db.all_auto_jobs()[0]['hour'], 20)
            async def earlier_time_with_window(**kwargs):
                return {**(await earlier_time_without_window()), 'read_end': '19:00'}
            request.json = earlier_time_with_window
            self.assertEqual((await plugin.page_auto_update())['id'], 1)
            saved_job = (await plugin.page_auto_jobs())['jobs'][0]
            self.assertEqual((saved_job['hour'], saved_job['minute'],
                              saved_job['read_end_minute']), (19, 0, 1140))
            async def changed_target(**kwargs):
                return {**(await add_payload()), 'id': 1, 'group_id': '222',
                        'daily_time': '21:00', 'read_end': '21:00'}
            request.json = changed_target
            edited = await plugin.page_auto_update()
            self.assertEqual(edited['id'], 1)
            self.assertEqual(edited['message'], '任务已更新；下次按新设置执行。')
            job = db.all_auto_jobs()[0]
            self.assertEqual((job['group_id'], job['hour'], job['cursor_id']),
                             ('222', 21, 0))
            self.assertEqual(job['last_status'], '待运行')
            async def delete_payload(**kwargs):
                return {'id': 1}
            request.json = delete_payload
            self.assertTrue((await plugin.page_auto_delete())['removed'])
            self.assertEqual((await plugin.page_auto_jobs())['jobs'], [])
        await plugin.terminate()

    async def test_natural_language_tool_obeys_private_scope(self):
        private = Event(group='')
        created = json.loads(await self.plugin.auto_manage_tool(
            private, '添加', '111', '20:00'))
        self.assertIn('已设置任务', created['message'])
        listed = json.loads(await self.plugin.auto_manage_tool(private, '列表'))
        self.assertIn('#1', listed['message'])
        denied = json.loads(await self.plugin.auto_manage_tool(
            Event(), '添加', '111', '20:00'))
        self.assertIn('error', denied)
        foreign = json.loads(await self.plugin.auto_manage_tool(
            Event(group='', user='43'), '删除', '1'))
        self.assertIn('error', foreign)
        self.assertEqual(len((await self.plugin.database()).all_auto_jobs()), 1)

    async def test_due_job_private_send_once_and_no_new_skips(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10,
                                       text='请大家核对报名时间'))
        await self.plugin.auto_tick(now)
        self.assertEqual(len(ctx.sent), 1)
        self.assertEqual(ctx.sent[0][0], 'aiocqhttp:FriendMessage:42')
        self.assertIn('自动群聊总结', ctx.sent[0][1])
        self.assertNotIn('**', ctx.sent[0][1])
        self.assertIn('【重要讨论】', ctx.sent[0][1])
        self.assertIn('请大家核对报名时间', ctx.prompts[-1])
        await self.plugin.auto_tick(now + 5)
        self.assertEqual(len(ctx.sent), 1)
        job = (await self.plugin.database()).all_auto_jobs()[0]
        self.assertGreater(job['cursor_id'], 0)
        self.assertEqual(job['last_status'], '已推送')

        second = main.GroupMemory(ctx, self.config)
        await second.auto_tick(now + 86400)
        self.assertEqual(len(ctx.sent), 1)
        self.assertEqual((await second.database()).all_auto_jobs()[0]['last_status'],
                         '无新增已保存记录，未推送')

    async def test_daily_window_summarizes_saved_records_even_after_prior_cursor(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        now = day + 21 * 3600 + 60
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 21, 1,
                                 day, day, 6 * 60, 19 * 60)
        for number, hour in enumerate((10, 17), 1):
            db.insert(dict(platform='qq-main', bot='999', group_id='111',
                           message_id=f'daily-{number}', timestamp=day + hour * 3600,
                           sender_id='1', sender_name='群友',
                           content=f'消息{number}', truncated=0, media_payload=''))
        db.finish_auto_job(job_id, '此前已推送', cursor_id=2, sent_at=day + 18 * 3600)
        await self.plugin.run_auto_job(db.all_auto_jobs()[0], now)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('消息1', ctx.prompts[-1])
        self.assertIn('消息2', ctx.prompts[-1])
        self.assertEqual(db.all_auto_jobs()[0]['last_status'], '已推送')

    async def test_daily_window_sends_empty_notice(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', 21, 1,
                        day, day, 6 * 60, 19 * 60)
        await self.plugin.run_auto_job(db.all_auto_jobs()[0], day + 21 * 3600 + 60)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('设定时段内没有已保存的聊天记录', ctx.sent[0][1])
        self.assertEqual(ctx.prompts, [])
        self.assertEqual(db.all_auto_jobs()[0]['last_status'],
                         '已推送：设定时段无已保存记录')
        self.assertEqual(db.all_auto_jobs()[0]['last_read_count'], 0)

    async def test_daily_previous_day_reads_selected_date(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        now = day + 86400 + 21 * 3600 + 60
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', 21, 1,
                        day, day, 8 * 60, 19 * 60, 'daily', 1)
        for number, stamp in enumerate((day + 10 * 3600,
                                        day + 86400 + 10 * 3600), 1):
            db.insert(dict(platform='qq-main', bot='999', group_id='111',
                           message_id=f'previous-{number}', timestamp=stamp,
                           sender_id='1', sender_name='群友',
                           content=f'日期{number}', truncated=0, media_payload=''))
        await self.plugin.run_auto_job(db.all_auto_jobs()[0], now)
        self.assertIn('日期1', ctx.prompts[-1])
        self.assertNotIn('日期2', ctx.prompts[-1])
        self.assertIn('前一天读取', ctx.sent[0][1])

    async def test_single_run_reads_fixed_date_and_coexists_with_daily_job(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        read_date, send_date = '2026-09-23', '2026-09-24'
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', 21, 1,
                        day, day, 8 * 60, 19 * 60)
        fixed_id = db.add_auto_job('qq-main', '999', '111', '42',
                                   'aiocqhttp:FriendMessage:42', 21, 1,
                                   day, day, 8 * 60, 19 * 60,
                                   'once', 0, read_date, send_date)
        self.assertEqual(len(db.all_auto_jobs()), 2)
        job = next(row for row in db.all_auto_jobs() if row['id'] == fixed_id)
        db.insert(dict(platform='qq-main', bot='999', group_id='111',
                       message_id='fixed-date', timestamp=day + 12 * 3600,
                       sender_id='1', sender_name='群友', content='固定日期消息',
                       truncated=0, media_payload=''))
        self.assertIsNone(due_state(job, day + 21 * 3600 + 60)[0])
        now = day + 86400 + 21 * 3600 + 60
        self.assertEqual(due_state(job, now)[0], 'run')
        await self.plugin.run_auto_job(job, now)
        self.assertIn('固定日期消息', ctx.prompts[-1])
        self.assertIn(read_date, ctx.sent[0][1])
        completed = next(row for row in db.all_auto_jobs() if row['id'] == fixed_id)
        self.assertEqual(completed['last_read_count'], 1)
        self.assertEqual(completed['last_read_start'], day + 8 * 3600)
        self.assertEqual(completed['last_read_end'], day + 19 * 3600)
        self.assertEqual(completed['last_read_partial'], 0)
        self.assertIsNone(due_state({**job, 'last_run_date': send_date}, now)[0])
        self.assertIsNone(due_state({**job, 'last_run_date': send_date}, now + 86400)[0])
        self.assertEqual(due_state(job, now + 86400)[0], 'skip')

    async def test_single_run_backfills_before_summary_and_deduplicates(self):
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        recent = dict(message_id=12, message_seq=12, time=day + 12 * 3600,
                      user_id=7, sender={'nickname': '成员'},
                      message=[{'type': 'text', 'data': {'text': '离线期间的话题'}}])
        saved = dict(message_id=13, message_seq=13, time=day + 13 * 3600,
                     user_id=7, sender={'nickname': '成员'},
                     message=[{'type': 'text', 'data': {'text': '原已保存的话题'}}])
        older = dict(message_id=11, message_seq=11, time=day + 7 * 3600,
                     user_id=7, sender={'nickname': '成员'},
                     message=[{'type': 'text', 'data': {'text': '窗口外'}}])

        class HistoryContext(AutoContext):
            def __init__(self):
                super().__init__()
                self.history_calls = 0

            async def call_action(self, name, **kwargs):
                if name == 'get_login_info':
                    return await super().call_action(name, **kwargs)
                self.history_calls += 1
                self.assertion_args = kwargs
                return {'status': 'ok', 'retcode': 0,
                        'data': {'messages': [saved, recent, older]}}

        ctx = self.plugin.context = HistoryContext()
        db = await self.plugin.database()
        db.insert(history_message(saved, 'qq-main', '999', '111', day, day + 86400))
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 21, 1,
                                 day, day, 8 * 60, 19 * 60,
                                 'once', 0, '2026-09-23', '2026-09-24')
        job = next(row for row in db.all_auto_jobs() if row['id'] == job_id)
        await self.plugin.run_auto_job(job, day + 86400 + 21 * 3600)
        self.assertEqual(ctx.history_calls, 1)
        self.assertIn('离线期间的话题', ctx.prompts[-1])
        self.assertIn('原已保存的话题', ctx.prompts[-1])
        self.assertIn('新增 1 条', ctx.sent[0][1])
        self.assertEqual(len(db.resolve_messages('qq-main', '999', '111', ['12'])), 1)

    async def test_daily_window_backfills_history_before_summary(self):
        day = datetime(2026, 9, 23, tzinfo=core.TZ).timestamp()
        saved = dict(message_id=13, message_seq=13, time=day + 13 * 3600,
                     user_id=7, sender={'nickname': '成员'},
                     message=[{'type': 'text', 'data': {'text': '已保存内容'}}])
        missed = dict(message_id=12, message_seq=12, time=day + 12 * 3600,
                      user_id=7, sender={'nickname': '成员'},
                      message=[{'type': 'text', 'data': {'text': '上午离线内容'}}])
        older = dict(message_id=11, message_seq=11, time=day + 7 * 3600,
                     user_id=7, sender={'nickname': '成员'}, message='时段外内容')

        class HistoryContext(AutoContext):
            def __init__(self):
                super().__init__()
                self.history_calls = 0

            async def call_action(self, name, **kwargs):
                if name == 'get_login_info':
                    return await super().call_action(name, **kwargs)
                self.history_calls += 1
                return {'status': 'ok', 'retcode': 0,
                        'data': {'messages': [saved, missed, older]}}

        ctx = self.plugin.context = HistoryContext()
        db = await self.plugin.database()
        db.insert(history_message(saved, 'qq-main', '999', '111', day, day + 86400))
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', 20, 0,
                        day, day, 8 * 60, 19 * 60)
        await self.plugin.run_auto_job(db.all_auto_jobs()[0], day + 20 * 3600)
        self.assertEqual(ctx.history_calls, 1)
        self.assertIn('上午离线内容', ctx.prompts[-1])
        self.assertIn('已保存内容', ctx.prompts[-1])
        self.assertNotIn('时段外内容', ctx.prompts[-1])
        self.assertIn('补读 1 页，新增 1 条', ctx.sent[0][1])
        self.assertEqual(db.all_auto_jobs()[0]['last_read_count'], 2)

    async def test_legacy_daily_job_backfills_since_last_delivery(self):
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        missed = dict(message_id=24, message_seq=24, time=now - 10,
                      user_id=7, sender={'nickname': '成员'},
                      message=[{'type': 'text', 'data': {'text': '离线补读内容'}}])
        older = dict(message_id=23, message_seq=23, time=now - 7200,
                     user_id=7, sender={'nickname': '成员'}, message='上次推送前')

        class HistoryContext(AutoContext):
            async def call_action(self, name, **kwargs):
                if name == 'get_login_info':
                    return await super().call_action(name, **kwargs)
                return {'status': 'ok', 'retcode': 0,
                        'data': {'messages': [missed, older]}}

        ctx = self.plugin.context = HistoryContext()
        db = await self.plugin.database()
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42',
                                 current.hour, current.minute, now - 86400,
                                 now - 120)
        db.finish_auto_job(job_id, '此前已推送', cursor_id=0,
                           sent_at=now - 3600)
        await self.plugin.run_auto_job(db.all_auto_jobs()[0], now)
        self.assertIn('离线补读内容', ctx.prompts[-1])
        self.assertNotIn('上次推送前', ctx.prompts[-1])
        self.assertEqual(db.all_auto_jobs()[0]['last_read_count'], 1)

    async def test_history_pagination_stops_when_anchor_does_not_advance(self):
        page = [dict(message_id=12, message_seq=12, time=120,
                     user_id=7, sender={'nickname': '成员'},
                     message=[{'type': 'image', 'data': {'file': 'f'}}])]

        class RepeatingClient:
            def __init__(self):
                self.calls = []

            async def call_action(self, name, **kwargs):
                self.calls.append(kwargs)
                return {'status': 'ok', 'retcode': 0, 'data': {'messages': page}}

        client = RepeatingClient()
        raw, status, pages = await fetch_history(client, '999', '111', 100, 200)
        self.assertEqual((len(raw), pages), (1, 3))
        self.assertIn('未继续前进', status)
        self.assertEqual(client.calls[1]['message_seq'], 12)
        self.assertEqual(client.calls[2]['message_id'], 12)
        converted = history_message(raw[0], 'qq-main', '999', '111', 100, 200)
        self.assertIn('图片', converted['content'])
        self.assertIn('image', converted['media_payload'])
        self.assertIsNone(history_message(
            dict(raw[0], group_id=222), 'qq-main', '999', '111', 100, 200))

    async def test_history_backfill_reads_past_twenty_pages(self):
        class Client:
            async def call_action(self, name, **kwargs):
                timestamp = int(kwargs.get('message_seq', 131)) - 1
                row = dict(message_id=timestamp, message_seq=timestamp,
                           time=timestamp, user_id=7,
                           sender={'nickname': '成员'}, message='记录')
                return {'status': 'ok', 'retcode': 0,
                        'data': {'messages': [row]}}

        raw, status, pages = await fetch_history(Client(), '999', '111', 100, 200)
        self.assertEqual(pages, 32)
        self.assertEqual(len(raw), 32)
        self.assertIn('读取起点', status)

    async def test_manual_backfill_does_not_generate_or_send(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        today = datetime.now(core.TZ).date()
        now = time.time()
        fixed_id = db.add_auto_job('qq-main', '999', '111', '42',
                                   'aiocqhttp:FriendMessage:42', 21, 1,
                                   now, now, 8 * 60, 19 * 60,
                                   'once', 0, today.isoformat(),
                                   (today + timedelta(days=1)).isoformat())
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        async def payload(**kwargs):
            return {'id': fixed_id}
        web.request = types.SimpleNamespace(json=payload)
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await self.plugin.page_auto_backfill()
        self.assertIn('补读', result['message'])
        self.assertFalse(ctx.prompts)
        self.assertFalse(ctx.sent)

    async def test_completed_once_job_cannot_backfill_as_if_pending(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        today = datetime.now(core.TZ).date()
        send_date = (today + timedelta(days=1)).isoformat()
        now = time.time()
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 21, 1,
                                 now, now, 8 * 60, 19 * 60,
                                 'once', 0, today.isoformat(), send_date)
        self.assertTrue(db.claim_auto_job(job_id, send_date))
        db.finish_auto_job(job_id, '已推送')
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        async def payload(**kwargs):
            return {'id': job_id}
        web.request = types.SimpleNamespace(json=payload)
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await self.plugin.page_auto_backfill()
        self.assertIn('不会重新生成总结', result['error'])
        self.assertEqual(db.all_auto_jobs()[0]['last_status'], '已推送')
        self.assertFalse(ctx.prompts)
        self.assertFalse(ctx.sent)

    async def test_history_uses_message_id_when_sequence_page_repeats(self):
        latest = dict(message_id=20, message_seq=20, time=200)
        older = dict(message_id=19, message_seq=19, time=90)

        class Client:
            async def call_action(self, name, **kwargs):
                messages = ([latest] if 'message_id' not in kwargs
                            else [latest, older])
                return {'status': 'ok', 'retcode': 0,
                        'data': {'messages': messages}}

        raw, status, pages = await fetch_history(Client(), '999', '111', 100, 300)
        self.assertEqual({row['message_id'] for row in raw}, {19, 20})
        self.assertEqual(pages, 3)
        self.assertIn('读取起点', status)

    async def test_date_options_validate_single_delivery_and_previous_day(self):
        now = datetime(2026, 9, 24, 10, 0, tzinfo=core.TZ).timestamp()
        self.assertEqual(main.auto_schedule_options(
            {'schedule_kind': 'daily', 'date_offset': 1}, now,
            8, 0, 9 * 60, 22 * 60, 30), ('daily', 1, '', ''))
        self.assertEqual(main.auto_schedule_options(
            {'schedule_kind': 'once', 'read_date': '2026-09-23',
             'send_date': '2026-09-25'}, now, 20, 0, 8 * 60, 19 * 60, 30),
            ('once', 0, '2026-09-23', '2026-09-25'))
        self.assertEqual(main.auto_schedule_options(
            {'schedule_kind': 'once', 'read_date': '2026-09-24',
             'send_date': '2026-09-24'}, now, 20, 0, 8 * 60, 10 * 60, 30),
            ('once', 0, '2026-09-24', '2026-09-24'))
        with self.assertRaisesRegex(ValueError, '晚于'):
            main.auto_schedule_options(
                {'schedule_kind': 'once', 'read_date': '2026-09-25',
                 'send_date': '2026-09-24'}, now, 20, 0, 8 * 60, 19 * 60, 30)
        with self.assertRaisesRegex(ValueError, '保存期'):
            main.auto_schedule_options(
                {'schedule_kind': 'once', 'read_date': '2026-08-01',
                 'send_date': '2026-09-25'}, now, 20, 0, 8 * 60, 19 * 60, 30)

    async def test_inclusive_end_covers_the_full_minute_without_changing_old_jobs(self):
        db = await self.plugin.database()
        day = datetime(2026, 9, 23, tzinfo=core.TZ)
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 20, 1,
                                 day.timestamp(), day.timestamp(), 8 * 60,
                                 20 * 60, read_end_inclusive=True)
        job = db.all_auto_jobs()[0]
        self.assertEqual(job['id'], job_id)
        self.assertEqual(job['read_end_inclusive'], 1)
        start, end = window_bounds(job, day.timestamp())
        self.assertEqual(end, day.timestamp() + 20 * 3600 + 60)
        for index, seconds in enumerate((20 * 3600, 20 * 3600 + 59,
                                         20 * 3600 + 60)):
            db.insert(dict(platform='qq-main', bot='999', group_id='111',
                           message_id=f'inclusive-{index}',
                           timestamp=day.timestamp() + seconds, sender_id='1',
                           sender_name='群友', content=str(index), truncated=0,
                           media_payload=''))
        rows, _ = db.auto_messages(job, day.timestamp(), 10, end, start)
        self.assertEqual([row['content'] for row in rows], ['0', '1'])
        legacy = {**job, 'read_end_inclusive': 0}
        self.assertEqual(window_bounds(legacy, day.timestamp())[1], end - 60)
        self.assertEqual(main.auto_schedule_options(
            {'schedule_kind': 'daily', 'date_offset': 0,
             'read_end_inclusive': True}, day.timestamp(),
            20, 0, 8 * 60, 19 * 60 + 59, 30), ('daily', 0, '', ''))
        with self.assertRaisesRegex(ValueError, '早于推送'):
            main.auto_schedule_options(
                {'schedule_kind': 'daily', 'date_offset': 0,
                 'read_end_inclusive': True}, day.timestamp(),
                20, 0, 8 * 60, 20 * 60, 30)
        overnight_start, overnight_end = window_bounds({
            'read_start_minute': 20 * 60, 'read_end_minute': 8 * 60,
            'read_end_inclusive': 1}, day.timestamp() + 86400)
        self.assertEqual(overnight_start, day.timestamp() + 20 * 3600)
        self.assertEqual(overnight_end, day.timestamp() + 86400 + 8 * 3600 + 60)

    async def test_model_failure_does_not_advance_cursor_or_send(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10))
        ctx.fail_model = True
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.sent, [])
        job = (await self.plugin.database()).all_auto_jobs()[0]
        self.assertEqual(job['cursor_id'], 0)
        self.assertEqual(job['last_status'], '失败：生成总结（RuntimeError）')
        ctx.fail_model = False
        await self.plugin.auto_tick(now + 86400)
        self.assertEqual(len(ctx.sent), 1)
        self.assertGreater((await self.plugin.database()).all_auto_jobs()[0]['cursor_id'], 0)

    async def test_delivery_failure_does_not_mark_messages_sent(self):
        ctx = self.plugin.context = AutoContext()
        async def no_route(umo, chain):
            return False
        ctx.send_message = no_route
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10))
        await self.plugin.auto_tick(now)
        job = (await self.plugin.database()).all_auto_jobs()[0]
        self.assertEqual(job['cursor_id'], 0)
        self.assertEqual(job['last_status'], '失败：发送QQ私聊（RuntimeError）')

    async def test_long_report_is_sent_as_one_qq_message(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        job = (await self.plugin.database()).all_auto_jobs()[0]
        report = '自动群聊总结\n' + '\n'.join('话题：' + str(i) + '。' * 180
                                            for i in range(12))
        self.assertGreater(len(report), 1200)
        await self.plugin.send_auto_private(job, report)
        self.assertEqual([item[1] for item in ctx.sent], [report])

    async def test_email_task_sends_complete_report_without_qq_delivery(self):
        ctx = self.plugin.context = AutoContext()
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='app-code')
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        db = await self.plugin.database()
        job_id = db.add_auto_job(
            'qq-main', '999', '111', '42', 'aiocqhttp:FriendMessage:42',
            current.hour, current.minute, now - 86400, now - 60,
            delivery_kind='email', email_to='owner@example.com')
        await self.plugin.record(Event(mid='mail-item', timestamp=now - 10,
                                       text='明天报名截止'))
        with mock.patch.object(main, 'send_email') as smtp_send:
            await self.plugin.auto_tick(now)
        self.assertEqual(len(ctx.sent), 0)
        smtp_send.assert_called_once()
        self.assertEqual(smtp_send.call_args.args[1], 'owner@example.com')
        self.assertIn('群聊 111', smtp_send.call_args.args[2])
        self.assertIn('明天报名截止', ctx.prompts[-1])
        self.assertTrue(ctx.provider_ids)
        self.assertTrue(all(value == 'default-model' for value in ctx.provider_ids))
        self.assertIn('【重要讨论】', smtp_send.call_args.args[3])
        job = next(row for row in db.all_auto_jobs() if row['id'] == job_id)
        self.assertEqual(job['last_status'], '已提交邮件')
        self.assertGreater(job['cursor_id'], 0)

    async def test_email_failure_does_not_advance_cursor_or_expose_secret(self):
        self.plugin.context = AutoContext()
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='private-code')
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        db = await self.plugin.database()
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', current.hour, current.minute,
                        now - 86400, now - 60, delivery_kind='email',
                        email_to='owner@example.com')
        await self.plugin.record(Event(mid='mail-fail', timestamp=now - 10))
        with mock.patch.object(main, 'send_email',
                               side_effect=smtplib.SMTPAuthenticationError(
                                   535, b'private-code')):
            await self.plugin.auto_tick(now)
        job = db.all_auto_jobs()[0]
        self.assertEqual(job['cursor_id'], 0)
        self.assertIn('SMTP 认证失败', job['last_status'])
        self.assertNotIn('private-code', job['last_status'])

    async def test_smtp_message_is_one_utf8_email_over_tls(self):
        settings = dict(smtp_host='smtp.example.com', smtp_port=465,
                        smtp_security='SSL/TLS', smtp_username='bot@example.com',
                        smtp_password='private-code')
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        with mock.patch('astrbot_plugin_group_memory.mailer.smtplib.SMTP_SSL',
                        return_value=connection) as smtp:
            send_email(settings, 'owner@example.com', '群聊总结', '完整报告\n第二行')
        self.assertEqual(smtp.call_args.args, ('smtp.example.com', 465))
        connection.login.assert_called_once_with('bot@example.com', 'private-code')
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message.get_body(preferencelist=('plain',)).get_content(),
                         '完整报告\n第二行\n')
        self.assertIn('<h1', message.get_body(preferencelist=('html',)).get_content())
        self.assertEqual(message['To'], 'owner@example.com')
        with self.assertRaises(ValueError):
            email_address('owner@example.com\r\nBcc: outsider@example.com')
        self.assertEqual(smtp_options(settings)[-1], 'bot@example.com')
        settings.update(smtp_security='STARTTLS', smtp_port=587)
        connection.reset_mock()
        with mock.patch('astrbot_plugin_group_memory.mailer.smtplib.SMTP',
                        return_value=connection) as smtp:
            send_email(settings, 'owner@example.com', '测试', '内容')
        self.assertEqual(smtp.call_args.args, ('smtp.example.com', 587))
        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with('bot@example.com', 'private-code')

    async def test_email_layout_highlights_discussions_and_resources(self):
        report = ('自动群聊总结｜群 123\n2026-09-24 08:00—20:00｜已读 20 条\n'
                  '【重要讨论】\n• API 价格与额度\n群友讨论新价格；官方未确认。\n'
                  '【有用资源】\n• 工具文档\n用途：查询接口。\n'
                  '链接：https://example.com/docs\n'
                  '【你的待办与提醒】\n无\n【待确认的信息】\n无\n'
                  '覆盖说明：图片 2 张未解析。')
        html = render_report_html(report)
        self.assertIn('<h2', html)
        self.assertIn('API 价格与额度</h3>', html)
        self.assertIn('工具文档</div>', html)
        self.assertIn('href="https://example.com/docs"', html)
        self.assertIn('http://[bad', render_report_html(
            '【重要讨论】\n• 异常链接\n群友发了 http://[bad 。\n'
            '【有用资源】\n无\n【你的待办与提醒】\n无\n【待确认的信息】\n无'))
        self.assertIn('图片 2 张未解析', html)
        self.assertNotIn('<script>', render_report_html(
            '自动群聊总结\n【重要讨论】\n• <script>alert(1)</script>\n内容'))
        self.assertIn('&lt;script&gt;', render_report_html(
            '自动群聊总结\n【重要讨论】\n• <script>alert(1)</script>\n内容'))

    async def test_report_check_keeps_traceable_resources_only(self):
        report = ('【重要讨论】\n• 价格调整\n群友讨论价格，尚无结论。\n'
                  '【有用资源】\n• 官方文档\n用途：查询接口。\n链接：https://example.com/docs\n'
                  '• 某模型\n群友说很好用。\n'
                  '• 虚构链接\n用途：查询接口。\n链接：https://fake.example/path\n'
                  '• 无说明链接\n链接：https://example.com/docs\n'
                  '【你的待办与提醒】\n无\n【待确认的信息】\n无')
        checked = checked_report(report, '群友分享 https://example.com/docs 可查询接口。')
        self.assertIn('官方文档', checked)
        self.assertNotIn('某模型', checked)
        self.assertNotIn('https://fake.example/path', checked)
        self.assertNotIn('无说明链接', checked)
        self.assertEqual(report_date_label('标题\n2026-09-23 08:00—2026-09-24 09:00'),
                         '2026-09-23—2026-09-24')
        with self.assertRaises(ValueError):
            checked_report('【重要讨论】\n没有标题\n【有用资源】\n无', '')

    async def test_report_keeps_source_backed_tool_with_specific_use(self):
        report = ('【重要讨论】\n• 模型使用\n群友讨论工具体验。\n'
                  '【有用资源】\n• CodeBuddy 混元4\n'
                  '群友称可用于代码审查；群内未提供链接。\n'
                  '【你的待办与提醒】\n无\n【待确认的信息】\n无')
        checked = checked_report(report, '群友称 CodeBuddy 混元4 可以做 code review。')
        self.assertIn('CodeBuddy 混元4', checked)

    async def test_malformed_report_is_repaired_once_before_delivery(self):
        ctx = self.plugin.context = AutoContext()
        calls = []

        async def generate(**kwargs):
            calls.append(kwargs['prompt'])
            if '格式未通过检查' in kwargs['prompt']:
                return types.SimpleNamespace(completion_text=(
                    '【重要讨论】\n• 报名安排\n群友讨论报名，具体时间待确认。\n'
                    '【有用资源】\n无\n【你的待办与提醒】\n无\n【待确认的信息】\n无'))
            return types.SimpleNamespace(completion_text='报名安排：群友讨论报名。')

        ctx.llm_generate = generate
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='repair-item', timestamp=now - 10,
                                       text='群友讨论报名'))
        await self.plugin.auto_tick(now)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('【重要讨论】', ctx.sent[0][1])

    async def test_email_subject_uses_read_date(self):
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='app-code')
        job = {'id': 1, 'group_id': '123456789', 'email_to': 'owner@example.com'}
        report = '自动群聊总结｜群 123456789\n2026-09-23 08:32—19:44｜已读 369 条'
        with mock.patch.object(main, 'send_email') as send:
            await self.plugin.send_auto_email(job, report)
        self.assertEqual(send.call_args.args[2], '群聊总结｜群聊 123456789｜2026-09-23')

    async def test_email_can_deliver_saved_text_when_qq_is_offline(self):
        ctx = self.plugin.context = AutoContext()
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='app-code')
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        db = await self.plugin.database()
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', current.hour, current.minute,
                        now - 86400, now - 60, delivery_kind='email',
                        email_to='owner@example.com')
        await self.plugin.record(Event(mid='offline-text', timestamp=now - 10,
                                       text='重要文字记录'))
        async def offline(*args, **kwargs):
            raise ConnectionError('QQ offline')
        ctx.call_action = offline
        with mock.patch.object(main, 'send_email') as smtp_send:
            await self.plugin.auto_tick(now)
        smtp_send.assert_called_once()
        self.assertIn('重要文字记录', ctx.prompts[-1])
        self.assertEqual(db.all_auto_jobs()[0]['last_status'], '已提交邮件')

    async def test_email_offline_marks_images_unread_without_waiting_for_qq(self):
        ctx = self.plugin.context = AutoContext()
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='app-code', media_vision_provider='vision-model')
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        db = await self.plugin.database()
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', current.hour, current.minute,
                        now - 86400, now - 60, delivery_kind='email',
                        email_to='owner@example.com')
        image = Event(mid='offline-image', timestamp=now - 10, text='', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/test'}}]})
        image.chain = []
        await self.plugin.record(image)
        async def offline(*args, **kwargs):
            raise ConnectionError('QQ offline')
        ctx.call_action = offline
        with mock.patch.object(main, 'send_email') as smtp_send:
            await self.plugin.auto_tick(now)
        self.assertEqual(ctx.image_calls, 0)
        self.assertIn('图片 1 张未识别：QQ接入不可用', smtp_send.call_args.args[3])

    async def test_page_add_and_test_email_task(self):
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='private-code')
        ctx = self.plugin.context = AutoContext()
        await self.plugin.record(Event(mid='email-page-source'))
        request = types.SimpleNamespace(json=lambda **kwargs: None)
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        web.request = request
        async def payload(**kwargs):
            return {'group_id': '111', 'platform': 'qq-main', 'bot': '999',
                    'daily_time': '20:00',
                    'read_start': '08:00', 'read_end': '20:00',
                    'delivery_kind': 'email', 'email_to': 'owner@example.com'}
        request.json = payload
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            created = await self.plugin.page_auto_add()
            self.assertEqual(created['id'], 1)
            listed = await self.plugin.page_auto_jobs()
            self.assertTrue(listed['email_ready'])
            self.assertEqual(listed['jobs'][0]['delivery_kind'], 'email')
            self.assertEqual(listed['jobs'][0]['reader_id'], 'email:owner@example.com')
            self.assertEqual(listed['jobs'][0]['email_to'], 'owner@example.com')
            async def second_recipient(**kwargs):
                return {**(await payload()), 'email_to': 'team@example.com'}
            request.json = second_recipient
            self.assertNotEqual((await self.plugin.page_auto_add())['id'], created['id'])
            self.assertEqual(len((await self.plugin.page_auto_jobs())['jobs']), 2)
            self.assertNotIn('private-code', str(listed))
            async def test_payload(**kwargs):
                return {'id': 1}
            request.json = test_payload
            with mock.patch.object(main, 'send_email') as smtp_send:
                result = await self.plugin.page_auto_test_send()
            self.assertTrue(result['sent'])
            smtp_send.assert_called_once()
            self.assertIn('测试', smtp_send.call_args.args[2])
            self.assertEqual(ctx.sent, [])

    async def test_group_alias_persists_and_changes_email_subject(self):
        db = await self.plugin.database()
        db.set_group_alias('111', '技术交流群')
        self.assertEqual(db.group_aliases({'111'}), {'111': '技术交流群'})
        self.assertEqual(await self.plugin.group_title('111'), '技术交流群（111）')
        restored = main.GroupMemory(None, self.config)
        self.assertEqual(await restored.group_title('111'), '技术交流群（111）')
        with self.assertRaisesRegex(ValueError, '控制字符'):
            main.clean_group_alias('错误\n名称')
        db.set_group_alias('111', '')
        self.assertEqual(db.group_aliases({'111'}), {})

    async def test_integrated_settings_save_and_secret_redaction(self):
        class SavedConfig(dict):
            saves = 0
            def save_config(self):
                self.saves += 1
        config = SavedConfig(self.config)
        config['smtp_password'] = 'existing-private-code'
        plugin = main.GroupMemory(None, config)
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        web.request = types.SimpleNamespace(json=lambda **kwargs: None)
        async def payload(**kwargs):
            return {'record_groups': ['111', '222', '111'], 'reader_qq_ids': ['42'],
                    'retention_days': 45, 'auto_summary_images': False,
                    'smtp_host': 'smtp.qq.com', 'smtp_port': 465,
                    'smtp_security': 'SSL/TLS',
                    'group_names': {'111': '学习交流群', '222': ''}}
        web.request.json = payload
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await plugin.page_settings_save()
            self.assertTrue(result['saved'])
            self.assertEqual(config.saves, 1)
            self.assertEqual(config['record_groups'], ['111', '222'])
            self.assertEqual(config['smtp_password'], 'existing-private-code')
            self.assertEqual((await plugin.page_settings_get())['group_names']['111'],
                             '学习交流群')
            self.assertEqual(await plugin.group_title('111'), '学习交流群（111）')
            exposed = await plugin.page_settings_get()
            self.assertTrue(exposed['settings']['smtp_password_set'])
            self.assertNotIn('existing-private-code', str(exposed))
            self.assertEqual((await plugin.page_settings_reveal_password())['smtp_password'],
                             'existing-private-code')
            async def clear_secret(**kwargs):
                return {'smtp_password_clear': True}
            web.request.json = clear_secret
            self.assertTrue((await plugin.page_settings_save())['saved'])
            self.assertEqual(config['smtp_password'], '')
            self.assertIn('尚未保存', (await plugin.page_settings_reveal_password())['error'])
            async def mismatched_names(**kwargs):
                return {'record_groups': ['111'], 'group_names': {'222': '错误绑定'}}
            web.request.json = mismatched_names
            self.assertIn('不一致', (await plugin.page_settings_save())['error'])
        self.assertEqual(public_settings(config)['retention_days'], 45)
        with self.assertRaisesRegex(ValueError, '未知'):
            validate_settings({'unexpected': 1})
        with self.assertRaisesRegex(ValueError, '数字'):
            validate_settings({'record_groups': ['not-a-group']})

    async def test_external_config_only_shows_working_plugin_log_level(self):
        schema = json.loads((Path(main.__file__).parent / '_conf_schema.json').read_text())
        self.assertEqual([key for key, value in schema.items()
                          if not value.get('invisible')], ['plugin_log_level'])
        with mock.patch.object(main.logger, 'info') as info, \
                mock.patch.object(main.logger, 'warning') as warning, \
                mock.patch.object(main.logger, 'error') as error:
            self.plugin.plugin_log('info', '开始')
            self.plugin.plugin_log('warning', '警告')
            self.plugin.plugin_log('error', '错误')
            info.assert_not_called()
            warning.assert_called_once_with('警告')
            error.assert_called_once_with('错误')
            self.config['plugin_log_level'] = '仅错误'
            self.plugin.plugin_log('warning', '静默警告')
            warning.assert_called_once()
            self.config['plugin_log_level'] = '详细'
            self.plugin.plugin_log('info', '开始')
            info.assert_called_once_with('开始')

    async def test_integrated_settings_save_failure_restores_runtime_config(self):
        class FailingConfig(dict):
            def save_config(self):
                raise OSError('disk unavailable')
        config = FailingConfig(self.config)
        plugin = main.GroupMemory(None, config)
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        async def payload(**kwargs):
            return {'retention_days': 60, 'smtp_password': 'private-value'}
        web.request = types.SimpleNamespace(json=payload)
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await plugin.page_settings_save()
        self.assertIn('保存插件设置失败', result['error'])
        self.assertEqual(config['retention_days'], 30)
        self.assertNotIn('smtp_password', config)
        self.assertNotIn('private-value', str(result))

    async def test_email_delivery_requires_smtp_and_valid_recipient(self):
        with self.assertRaisesRegex(ValueError, 'SMTP 服务器'):
            main.auto_delivery_options({'delivery_kind': 'email',
                                        'email_to': 'owner@example.com'}, self.config)
        self.config.update(smtp_host='smtp.example.com', smtp_port=465,
                           smtp_security='SSL/TLS', smtp_username='bot@example.com',
                           smtp_password='app-code')
        with self.assertRaisesRegex(ValueError, '邮箱地址'):
            main.auto_delivery_options({'delivery_kind': 'email',
                                        'email_to': 'not-an-address'}, self.config)

    async def test_qq_mail_sender_mismatch_and_refusal_are_specific(self):
        settings = dict(smtp_host='smtp.qq.com', smtp_port=465,
                        smtp_security='SSL/TLS', smtp_username='bot@qq.com',
                        smtp_password='private-code', smtp_sender='other@qq.com')
        with self.assertRaisesRegex(ValueError, '须与 SMTP 用户名相同'):
            smtp_options(settings)
        settings['smtp_sender'] = ''
        self.assertEqual(smtp_options(settings)[-1], 'bot@qq.com')
        refused = smtplib.SMTPSenderRefused(
            553, b'private-code and sensitive server text', 'other@qq.com')
        detail = smtp_error_detail(refused)
        self.assertIn('SMTP 拒绝发件地址（代码 553）', detail)
        self.assertNotIn('private-code', detail)
        self.assertNotIn('other@qq.com', detail)

    async def test_send_error_reports_qq_code_without_message_content(self):
        class ActionFailed(Exception):
            info = {'retcode': 1200, 'wording': 'Get Uid Error'}
        detail = main.qq_send_error_detail(ActionFailed('secret report text'))
        self.assertIn('retcode 1200', detail)
        self.assertIn('互加好友', detail)
        self.assertNotIn('secret report text', detail)

    async def test_page_short_private_send_checks_the_actual_route(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        db.add_auto_job('qq-main', '999', '111', '42',
                        'aiocqhttp:FriendMessage:42', 20, 0,
                        time.time() - 86400, time.time())
        request = types.SimpleNamespace(json=lambda **kwargs: None)
        web = types.ModuleType('astrbot.api.web')
        web.json_response = lambda value: value
        web.error_response = lambda message, **kwargs: {'error': message}
        web.request = request
        async def payload(**kwargs):
            return {'id': 1}
        request.json = payload
        with mock.patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await self.plugin.page_auto_test_send()
            self.assertTrue(result['sent'])
            self.assertIn('推送测试', ctx.sent[0][1])
            class ActionFailed(Exception):
                info = {'retcode': 1200, 'wording': 'Get Uid Error'}
            async def failed_send(umo, chain):
                raise ActionFailed('private content')
            ctx.send_message = failed_send
            failure = await self.plugin.page_auto_test_send()
            self.assertIn('retcode 1200', failure['error'])
            self.assertNotIn('private content', failure['error'])

    async def test_qq_offline_before_generation_retries_within_window(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10))
        connected = ctx.call_action
        async def offline(name, **kwargs):
            raise ConnectionError('QQ disconnected')
        ctx.call_action = offline
        await self.plugin.auto_tick(now)
        job = (await self.plugin.database()).all_auto_jobs()[0]
        self.assertEqual(job['last_run_date'], '')
        self.assertIn('QQ接入不可用', job['last_status'])
        self.assertEqual(ctx.prompts, [])
        ctx.call_action = connected
        await self.plugin.auto_tick(now + 61)
        self.assertEqual(len(ctx.sent), 1)

    async def test_qq_offline_after_generation_retries_before_sending(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10))
        connected = ctx.call_action
        login_calls = 0
        async def disconnect_before_send(name, **kwargs):
            nonlocal login_calls
            if name == 'get_login_info':
                login_calls += 1
                if login_calls == 3:
                    raise ConnectionError('QQ disconnected')
            return await connected(name, **kwargs)
        ctx.call_action = disconnect_before_send
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.sent, [])
        self.assertIn('等待重试', (await self.plugin.database()).all_auto_jobs()[0]['last_status'])
        ctx.call_action = connected
        await self.plugin.auto_tick(now + 61)
        self.assertEqual(len(ctx.sent), 1)

    async def test_revoked_reader_is_not_sent_a_report(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        await self.plugin.record(Event(mid='fresh', timestamp=now - 10))
        original = ctx.llm_generate
        async def revoke_during_model(**kwargs):
            response = await original(**kwargs)
            self.config['reader_qq_ids'] = []
            return response
        ctx.llm_generate = revoke_during_model
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.sent, [])
        job = (await self.plugin.database()).all_auto_jobs()[0]
        self.assertEqual(job['cursor_id'], 0)
        self.assertIn('不再授权', job['last_status'])

    async def test_auto_summary_reads_more_than_one_thousand_messages(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        db = await self.plugin.database()
        db.insert_history([
            dict(platform='qq-main', bot='999', group_id='111',
                 message_id=str(number + 1), timestamp=now - 20 + number / 100,
                 sender_id='1', sender_name='群友', content=f'事项 {number + 1}',
                 truncated=0, media_payload='')
            for number in range(1103)
        ])
        await self.plugin.auto_tick(now)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('已读 1103 条', ctx.sent[0][1])
        self.assertNotIn('（部分）', ctx.sent[0][1])
        self.assertTrue(any('事项 1103' in prompt for prompt in ctx.prompts))
        job = db.all_auto_jobs()[0]
        self.assertEqual(job['last_read_count'], 1103)
        self.assertEqual(job['last_read_partial'], 0)

    async def test_automatic_images_use_vision_without_local_files(self):
        ctx = self.plugin.context = AutoContext()
        self.config['media_vision_provider'] = 'vision-model'
        now = await self.add_due_job()
        image = Event(mid='image', timestamp=now - 10, text='', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'at', 'data': {'qq': '42'}},
                {'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/test'}}]})
        image.chain = []
        await self.plugin.record(image)
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.image_calls, 1)
        self.assertIn('截图中的报名信息', ctx.prompts[-1])
        self.assertIn('图片共 1 张，已处理 1 张，识别成功 1 张', ctx.prompts[-1])
        self.assertIn('[明确@收件人]', ctx.prompts[-1])

    async def test_automatic_summary_reads_all_images_without_a_count_setting(self):
        ctx = self.plugin.context = AutoContext()
        self.config['media_vision_provider'] = 'vision-model'
        now = await self.add_due_job()
        images = [{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/test' + str(i)}}
                  for i in range(5)]
        event = Event(mid='five-images', timestamp=now - 10, text='', raw_message={
            'post_type': 'message', 'message': images})
        event.chain = []
        await self.plugin.record(event)
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.image_calls, 5)
        self.assertIn('图片 5 张：识别成功 5、失败 0', ctx.sent[0][1])
        self.assertNotIn('未尝试', ctx.sent[0][1])

    async def test_automatic_image_toggle_disables_only_automatic_vision(self):
        ctx = self.plugin.context = AutoContext()
        self.config['media_vision_provider'] = 'vision-model'
        self.config['auto_summary_images'] = False
        now = await self.add_due_job()
        image = Event(mid='image-disabled', timestamp=now - 10, text='', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/test'}}]})
        image.chain = []
        await self.plugin.record(image)
        await self.plugin.auto_tick(now)
        self.assertEqual(ctx.image_calls, 0)
        self.assertIn('图片 1 张未识别：自动识图已关闭', ctx.sent[0][1])

    async def test_automatic_image_result_is_reused_without_new_model_call(self):
        ctx = self.plugin.context = AutoContext()
        self.config['media_vision_provider'] = 'vision-model'
        now = await self.add_due_job()
        image = Event(mid='cached-image', timestamp=now - 10, text='', raw_message={
            'post_type': 'message', 'message': [
                {'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/test'}}]})
        image.chain = []
        await self.plugin.record(image)
        db = await self.plugin.database()
        job = db.all_auto_jobs()[0]
        for run in range(2):
            rows, _ = db.auto_messages(job, now - 3600, 10)
            await self.plugin.auto_read_images(rows, job, ctx)
            self.assertEqual(rows[0]['auto_image_recognized'], 1)
            if run == 1:
                self.assertEqual(rows[0]['auto_image_cached'], 1)
        self.assertEqual(ctx.image_calls, 1)

    def test_plain_qq_format_and_cross_day_span(self):
        report = plain_report('**重要讨论**\n**话题**\n见[资料](https://example.com/x)。'
                              '\n## 有用资源\n[https://example.com](https://example.com)')
        self.assertIn('【重要讨论】\n• 话题', report)
        self.assertIn('资料：https://example.com/x', report)
        self.assertNotIn('**', report)
        self.assertNotIn('](', report)
        start = datetime(2026, 9, 23, 23, 0, tzinfo=core.TZ).timestamp()
        end = start + 7200
        self.assertEqual(short_span(start, end), '2026-09-23 23:00—2026-09-24 01:00')

    async def test_daily_window_keeps_prior_day_backlog(self):
        db = await self.plugin.database()
        day = datetime(2026, 9, 22, tzinfo=core.TZ)
        initial = day.timestamp()
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 10, 30,
                                 initial, initial, 8 * 60, 10 * 60)
        for index, (offset, body) in enumerate(((7 * 3600 + 59 * 60, '窗外'),
                                                 (8 * 3600 + 15 * 60, '首日窗内'),
                                                 (10 * 3600, '结束后'),
                                                 (86400 + 8 * 3600 + 30 * 60, '次日窗内'))):
            db.insert(dict(platform='qq-main', bot='999', group_id='111',
                           message_id=f'window-{index}', timestamp=initial + offset,
                           sender_id='1', sender_name='群友', content=body,
                           truncated=0, media_payload=''))
        job = db.all_auto_jobs()[0]
        self.assertEqual(job['id'], job_id)
        end = datetime(2026, 9, 23, 10, 0, tzinfo=core.TZ).timestamp()
        first, more = db.auto_messages(job, initial, 1, end)
        self.assertEqual([row['content'] for row in first], ['首日窗内'])
        self.assertTrue(more)
        job['cursor_id'] = first[0]['id']
        second, more = db.auto_messages(job, initial, 1, end)
        self.assertEqual([row['content'] for row in second], ['次日窗内'])
        self.assertFalse(more)

    async def test_overnight_window_and_no_duplicate_update(self):
        db = await self.plugin.database()
        now = datetime(2026, 9, 23, 8, 30, tzinfo=core.TZ).timestamp()
        start, end = window_bounds({'read_start_minute': 20 * 60,
                                    'read_end_minute': 8 * 60}, now)
        self.assertEqual(short_span(start, end), '2026-09-22 20:00—2026-09-23 08:00')
        first_id = db.add_auto_job('qq-main', '999', '111', '42',
                                   'aiocqhttp:FriendMessage:42', 8, 30, start,
                                   now - 3600, 20 * 60, 8 * 60)
        second_id = db.add_auto_job('qq-main', '999', '111', '42',
                                    'aiocqhttp:FriendMessage:42', 8, 30, start,
                                    now, 21 * 60, 8 * 60)
        self.assertEqual(first_id, second_id)
        self.assertEqual(db.all_auto_jobs()[0]['read_start_minute'], 21 * 60)
        for index, (hour, minute, day_offset) in enumerate(((22, 0, -1), (7, 0, 0),
                                                              (8, 5, 0))):
            timestamp = datetime(2026, 9, 23 + day_offset, hour, minute,
                                 tzinfo=core.TZ).timestamp()
            db.insert(dict(platform='qq-main', bot='999', group_id='111',
                           message_id=f'overnight-{index}', timestamp=timestamp,
                           sender_id='1', sender_name='群友', content=f'消息{index}',
                           truncated=0, media_payload=''))
        job = db.all_auto_jobs()[0]
        rows, more = db.auto_messages(job, start, 10, end)
        self.assertEqual([row['content'] for row in rows], ['消息0', '消息1'])
        self.assertFalse(more)

    async def test_windowed_auto_report_excludes_outside_messages(self):
        ctx = self.plugin.context = AutoContext()
        now = time.time()
        current = datetime.fromtimestamp(now, core.TZ)
        end_minute = current.hour * 60 + current.minute
        start_minute = (end_minute - 60) % 1440
        initial = now - 86400
        db = await self.plugin.database()
        db.add_auto_job('qq-main', '999', '111', '42', 'aiocqhttp:FriendMessage:42',
                        current.hour, current.minute, initial, now - 60,
                        start_minute, end_minute)
        await self.plugin.record(Event(mid='inside-window', timestamp=now - 1800,
                                       text='窗内的重要通知'))
        await self.plugin.record(Event(mid='outside-window', timestamp=now - 7200,
                                       text='窗外的闲聊'))
        await self.plugin.auto_tick(now)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('窗内的重要通知', ctx.prompts[-1])
        self.assertNotIn('窗外的闲聊', ctx.prompts[-1])
        self.assertIn('当天读取', ctx.sent[0][1])

    async def test_existing_schedule_database_gains_nullable_window_columns(self):
        path = Path(self.tmp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''CREATE TABLE auto_summary_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL, bot TEXT NOT NULL, group_id TEXT NOT NULL,
                reader_id TEXT NOT NULL, private_umo TEXT NOT NULL,
                hour INTEGER NOT NULL, minute INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_date TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '待运行',
                last_sent_at REAL, created_at REAL NOT NULL,
                initial_lower REAL NOT NULL, cursor_id INTEGER NOT NULL DEFAULT 0,
                UNIQUE(platform,bot,group_id,reader_id,hour,minute)
            )''')
            db.execute('''INSERT INTO auto_summary_jobs
                (platform,bot,group_id,reader_id,private_umo,hour,minute,
                 created_at,initial_lower) VALUES (?,?,?,?,?,?,?,?,?)''',
                ('qq-main', '999', '111', '42', 'aiocqhttp:FriendMessage:42',
                 20, 0, 1, 1))
        archive = base.store.Archive(path)
        old_job = archive.all_auto_jobs()[0]
        self.assertIsNone(old_job['read_start_minute'])
        self.assertIsNone(old_job['read_end_minute'])
        self.assertEqual(old_job['delivery_kind'], 'qq')
        self.assertEqual(old_job['email_to'], '')
        self.assertIsNone(old_job['last_read_count'])
        self.assertIsNone(old_job['last_read_start'])

    async def test_large_input_uses_bounded_chunk_summaries(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        for index in range(10):
            await self.plugin.record(Event(mid=f'long-{index}', timestamp=now - 20 + index,
                                           text=f'重要记录 {index} ' + '详' * 2500))
        await self.plugin.auto_tick(now)
        self.assertGreater(len(ctx.prompts), 2)
        self.assertIn('事实笔记', ctx.prompts[-1])
        self.assertEqual(len(ctx.sent), 1)

    async def test_long_input_extracts_two_chunks_concurrently(self):
        ctx = self.plugin.context = AutoContext()
        now = await self.add_due_job()
        for index in range(10):
            await self.plugin.record(Event(mid=f'parallel-{index}', timestamp=now - 20 + index,
                                           text='重要记录 ' + str(index) + ' ' + '详' * 2500))
        original = ctx.llm_generate
        active = peak = 0
        both_started = asyncio.Event()

        async def measured_generate(**kwargs):
            nonlocal active, peak
            if '事实笔记' not in kwargs['prompt'] or '第 ' not in kwargs['prompt']:
                return await original(**kwargs)
            active += 1
            peak = max(peak, active)
            if active >= 2:
                both_started.set()
            try:
                await asyncio.wait_for(both_started.wait(), 1)
                return await original(**kwargs)
            finally:
                active -= 1

        ctx.llm_generate = measured_generate
        await self.plugin.auto_tick(now)
        self.assertEqual(peak, 2)
        self.assertEqual(len(ctx.sent), 1)

    async def test_scheduler_lifecycle_and_missed_window(self):
        await self.plugin.start_auto_scheduler()
        self.assertIsNotNone(self.plugin.auto_task)
        await self.plugin.terminate()
        self.assertIsNone(self.plugin.auto_task)
        self.assertEqual(parse_time('20:00'), (20, 0))
        fixed = datetime(2026, 9, 23, 20, 0, tzinfo=core.TZ).timestamp()
        job = {'hour': 20, 'minute': 0, 'last_run_date': ''}
        self.assertEqual(due_state(job, fixed)[0], 'run')
        self.assertEqual(due_state(job, fixed + 1801)[0], 'run')
        self.assertEqual(due_state(job, fixed + 12 * 3600)[0], 'run')
        self.assertEqual(due_state(job, fixed + 14 * 3600 + 1)[0], 'skip')
        job['created_at'] = fixed + 300
        self.assertIsNone(due_state(job, fixed + 300)[0])
        self.assertIsNone(due_state(job, fixed + 12 * 3600)[0])

    async def test_missed_daily_window_can_catch_up_next_morning_once(self):
        ctx = self.plugin.context = AutoContext()
        db = await self.plugin.database()
        today = datetime.now(core.TZ).replace(hour=0, minute=0,
                                              second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        planned = yesterday.replace(hour=20).timestamp()
        job_id = db.add_auto_job('qq-main', '999', '111', '42',
                                 'aiocqhttp:FriendMessage:42', 20, 0,
                                 yesterday.timestamp(), planned - 3600,
                                 6 * 60, 20 * 60)
        db.insert(dict(platform='qq-main', bot='999', group_id='111',
                       message_id='yesterday-message',
                       timestamp=yesterday.replace(hour=12).timestamp(),
                       sender_id='1', sender_name='群友', content='昨日重要内容',
                       truncated=0, media_payload=''))
        db.claim_auto_job(job_id, yesterday.date().isoformat())
        db.finish_auto_job(job_id, '错过计划时间，未推送')
        await self.plugin.auto_tick(today.replace(hour=8).timestamp())
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn('昨日重要内容', ctx.prompts[-1])
        self.assertIn(yesterday.date().isoformat(), ctx.sent[0][1])
        job = db.all_auto_jobs()[0]
        self.assertEqual(job['last_status'], '已补发')
        self.assertEqual(job['last_run_date'], yesterday.date().isoformat())
        await self.plugin.auto_tick(today.replace(hour=9).timestamp())
        self.assertEqual(len(ctx.sent), 1)
