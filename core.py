"""Validation, bounded transcript serialization, and query cursors."""
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8), name='Asia/Shanghai')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def display_time(timestamp):
    return datetime.fromtimestamp(timestamp, TZ).isoformat(timespec='seconds')


def time_range(period, start, end, now):
    current = datetime.fromtimestamp(now, TZ)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if start or end:
        if not start or not end:
            raise ValueError('自定义时间必须同时提供 start 和 end。')
        def parse(value):
            dt = datetime.fromisoformat(value.removesuffix('Z') + '+00:00' if value.endswith('Z') else value)
            return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp()
        lower, upper = parse(start), parse(end)
    elif period in ('今天', 'today', ''):
        lower, upper = midnight.timestamp(), now
    elif period in ('昨天', 'yesterday'):
        lower, upper = (midnight - timedelta(days=1)).timestamp(), midnight.timestamp()
    else:
        match = re.fullmatch(r'最近(\d+)(小时|天)', period)
        if not match:
            raise ValueError('period 支持 今天、昨天、最近3小时、最近7天；或用 start/end。')
        amount = int(match[1])
        lower, upper = now - amount * (3600 if match[2] == '小时' else 86400), now
    if lower >= upper or upper - lower > 31 * 86400:
        raise ValueError('时间范围必须大于零且不超过31天，end 为不包含的结束时间。')
    return lower, upper


class Cursors:
    def __init__(self, secret):
        self.secret = secret

    def encode(self, payload):
        raw = dumps(payload).encode()
        sig = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig + raw).decode()

    def decode(self, token):
        try:
            if len(token) > 4096:
                raise ValueError()
            packed = base64.b64decode(token, altchars=b'-_', validate=True)
            sig, raw = packed[:32], packed[32:]
            if not hmac.compare_digest(sig, hmac.new(self.secret, raw, hashlib.sha256).digest()):
                raise ValueError()
            return json.loads(raw)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError('分页游标无效或插件已重启，请从第一页重新查询。') from exc


def transcript(event, max_chars=4000):
    parts = []
    for segment in event.get_messages():
        kind = type(segment).__name__
        if kind == 'Plain':
            text = getattr(segment, 'text', '')
            if text is not None:
                parts.append(str(text))
        elif kind == 'At':
            parts.append('@' + str(segment.qq))
        elif kind == 'AtAll':
            parts.append('@全体成员')
        elif kind == 'Reply':
            parts.append('[引用消息ID:' + str(getattr(segment, 'id', '')) + ']')
        else:
            label = {'Image': '图片', 'Record': '语音', 'Video': '视频',
                     'File': '文件', 'Forward': '合并转发', 'Node': '转发节点',
                     'Nodes': '合并转发', 'Face': '表情'}.get(kind, '非文本消息')
            parts.append('[' + label + '，未解析内容]')
    text = ''.join(parts).strip()
    if not text:
        text = str(event.message_str or '').strip()
    if not text:
        text = raw_transcript(getattr(event.message_obj, 'raw_message', None))
    if not text:
        text = '[空消息]'
    return text[:max_chars], len(text) > max_chars


def raw_value(raw, key, default=None):
    """Read a field from aiocqhttp Event objects or plain dictionaries."""
    if raw is None:
        return default
    try:
        return raw.get(key, default)
    except (AttributeError, TypeError):
        return getattr(raw, key, default)


def is_chat_message(event):
    """Reject OneBot notices/requests that AstrBot exposes as GROUP_MESSAGE."""
    raw = getattr(event.message_obj, 'raw_message', None)
    post_type = raw_value(raw, 'post_type')
    return post_type in (None, 'message', 'message_sent')


def raw_transcript(raw):
    """Recover text/placeholders from the original OneBot message payload."""
    message = raw_value(raw, 'message')
    parts = []
    if isinstance(message, list):
        labels = {
            'image': '图片', 'record': '语音', 'video': '视频', 'file': '文件',
            'forward': '合并转发', 'face': '表情', 'reply': '引用消息',
        }
        for segment in message:
            if not isinstance(segment, dict):
                continue
            kind = segment.get('type', '')
            data = segment.get('data') or {}
            if not isinstance(data, dict):
                data = {}
            if kind in ('text', 'plain'):
                value = data.get('text', '')
                if value is not None:
                    parts.append(str(value))
            elif kind == 'at':
                parts.append('@' + str(data.get('qq', '')))
            elif kind == 'mface':
                summary = str(data.get('summary') or '').strip('[] ')
                emoji_id = str(data.get('emoji_id') or '').strip()
                detail = summary or emoji_id
                parts.append('[商城表情' + (':' + detail if detail else '') + ']')
            elif kind == 'face':
                face_id = str(data.get('id') or '').strip()
                parts.append('[QQ表情' + (':' + face_id if face_id else '') + ']')
            elif kind == 'dice':
                result = str(data.get('result') or '').strip()
                parts.append('[骰子' + (':' + result if result else '') + ']')
            elif kind == 'rps':
                result = str(data.get('result') or '').strip()
                parts.append('[猜拳' + (':' + result if result else '') + ']')
            elif kind in ('poke', 'shake'):
                parts.append('[戳一戳]')
            elif kind == 'markdown':
                value = data.get('markdown') or data.get('content') or ''
                parts.append(str(value) if value else '[Markdown消息]')
            elif kind in ('json', 'xml'):
                parts.append('[' + kind.upper() + '卡片]')
            elif kind in ('music', 'share', 'contact', 'location'):
                parts.append('[分享卡片:' + kind + ']')
            elif kind in labels:
                parts.append('[' + labels[kind] + ']')
            elif kind:
                parts.append('[OneBot消息段:' + str(kind) + ']')
    text = ''.join(parts).strip()
    if text:
        return text
    raw_message = raw_value(raw, 'raw_message', '')
    value = str(raw_message).strip() if raw_message is not None else ''
    return '' if value.lower() in ('null', 'none') else value
