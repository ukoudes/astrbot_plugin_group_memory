"""Best-effort OneBot history import. API history is not proof of offline coverage."""
import asyncio

from .core import raw_transcript
from .media import sanitize
from .core import dumps

PAGE_SIZE = 100


def _timestamp(raw):
    try:
        return float(raw.get('time') or 0)
    except (TypeError, ValueError):
        return 0


def history_message(raw, platform, bot, group, lower, upper):
    if not isinstance(raw, dict):
        return None
    # Do not trust a history response to contain only the requested group.
    returned_group = raw.get('group_id')
    if returned_group is not None and str(returned_group) != str(group):
        return None
    timestamp = _timestamp(raw)
    if not lower <= timestamp < upper:
        return None
    mid = str(raw.get('message_id') or '')
    if not mid:
        return None
    sender = raw.get('sender') or {}
    if not isinstance(sender, dict):
        sender = {}
    sender_id = str(raw.get('user_id') or sender.get('user_id') or '')
    if sender_id == bot or not sender_id:
        return None
    name = str(sender.get('card') or sender.get('nickname') or sender_id)[:100]
    content = raw_transcript(raw)
    if not content and isinstance(raw.get('message'), str):
        content = raw['message'].strip()
    content = content or '[空消息]'
    segments = raw.get('message')
    payload = dumps(sanitize(segments)) if isinstance(segments, list) else ''
    return dict(platform=platform, bot=bot, group_id=group,
                message_id=mid, timestamp=timestamp, sender_id=sender_id,
                sender_name=name, content=content[:4000],
                truncated=int(len(content) > 4000), media_payload=payload)


async def fetch_history(client, bot, group, lower, upper,
                        max_pages=None):
    """Return (raw messages, status, pages); never assert absence of gaps."""
    if client is None:
        return [], 'QQ接入不可用', 0
    found, seen = [], set()
    anchor = None
    anchor_message_id = None
    anchor_key = 'message_seq'
    pages = 0
    status = '接口未能回溯至读取起点'
    while max_pages is None or pages < max_pages:
        args = dict(self_id=bot, group_id=int(group), count=PAGE_SIZE,
                    reverseOrder=True)
        if anchor is not None:
            args[anchor_key] = (anchor_message_id if anchor_key == 'message_id'
                                else anchor)
        try:
            response = await asyncio.wait_for(
                client.call_action('get_group_msg_history', **args), 12)
        except asyncio.TimeoutError:
            status = '群历史接口超时'
            break
        except Exception as exc:
            if anchor is not None and anchor_key == 'message_seq' and anchor_message_id:
                anchor_key = 'message_id'
                continue
            status = '群历史接口失败（' + type(exc).__name__ + '）'
            break
        if not isinstance(response, dict) or response.get('status') == 'failed' or response.get('retcode', 0) != 0:
            if anchor is not None and anchor_key == 'message_seq' and anchor_message_id:
                anchor_key = 'message_id'
                continue
            status = '群历史接口返回失败'
            break
        data = response.get('data', response)
        messages = data.get('messages') if isinstance(data, dict) else None
        if not isinstance(messages, list):
            status = '群历史接口返回格式异常'
            break
        pages += 1
        if not messages:
            status = '接口没有返回更早记录'
            break
        valid = [m for m in messages if isinstance(m, dict) and _timestamp(m) > 0]
        if not valid:
            status = '群历史接口返回格式异常'
            break
        oldest = min(valid, key=_timestamp)
        new_anchor = (oldest.get('message_seq') or oldest.get('real_id')
                      or oldest.get('seq') or oldest.get('message_id'))
        added_this_page = 0
        for item in valid:
            mid = str(item.get('message_id') or '')
            if mid and mid not in seen:
                seen.add(mid)
                found.append(item)
                added_this_page += 1
        if _timestamp(oldest) < lower:
            status = '已回溯到读取起点；离线期间仍可能有缺口'
            break
        if not new_anchor or str(new_anchor) == str(anchor) or not added_this_page:
            if anchor_key == 'message_seq' and anchor_message_id:
                anchor_key = 'message_id'
                continue
            status = '分页未继续前进，较早记录可能缺失'
            break
        anchor = new_anchor
        anchor_message_id = oldest.get('message_id')
    else:
        status = '达到补读页数上限，较早记录未确认'
    return found, status, pages
