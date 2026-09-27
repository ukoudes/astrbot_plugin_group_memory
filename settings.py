"""Validate the small set of plugin settings editable from the plugin Page."""

import re


LIST_KEYS = {'record_groups', 'reader_qq_ids', 'webchat_reader_ids'}
BOOL_KEYS = {'enable_media_read', 'auto_summary_images', 'enable_webchat'}
INT_RANGES = {'retention_days': (1, 3650), 'smtp_port': (1, 65535),
              'media_batch_concurrency': (1, 4),
              'media_get_image_timeout_seconds': (5, 60)}
TEXT_KEYS = {'media_vision_provider', 'smtp_host', 'smtp_security',
             'smtp_username', 'smtp_sender', 'summary_media_scope',
             'webchat_qq_platform_id', 'webchat_qq_bot_id'}
PUBLIC_KEYS = LIST_KEYS | BOOL_KEYS | set(INT_RANGES) | TEXT_KEYS


def public_settings(config):
    values = {key: config.get(key) for key in PUBLIC_KEYS}
    values['smtp_password_set'] = bool(config.get('smtp_password'))
    return values


def validate_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError('设置格式错误')
    extra = set(payload) - PUBLIC_KEYS - {'smtp_password', 'smtp_password_clear'}
    if extra:
        raise ValueError('包含未知设置项')
    values = {}
    for key in LIST_KEYS & payload.keys():
        items = payload[key]
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError('列表设置格式错误或条目过多')
        clean = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError('列表内容应为文字')
            item = item.strip()
            if not item:
                continue
            if len(item) > 100 or any(ord(char) < 32 for char in item):
                raise ValueError('列表内容过长或包含控制字符')
            if key in ('record_groups', 'reader_qq_ids') and not item.isascii():
                raise ValueError('QQ 群号和 QQ 号应使用数字')
            if key in ('record_groups', 'reader_qq_ids') and not item.isdigit():
                raise ValueError('QQ 群号和 QQ 号应使用数字')
            if item not in clean:
                clean.append(item)
        values[key] = clean
    for key in BOOL_KEYS & payload.keys():
        if not isinstance(payload[key], bool):
            raise ValueError(f'{key} 应为开或关')
        values[key] = payload[key]
    for key in INT_RANGES.keys() & payload.keys():
        value = payload[key]
        low, high = INT_RANGES[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} 应在 {low}～{high} 范围内')
        values[key] = value
    for key in TEXT_KEYS & payload.keys():
        value = payload[key]
        if not isinstance(value, str):
            raise ValueError(f'{key} 应为文字')
        value = value.strip()
        if len(value) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f'{key} 过长或包含控制字符')
        values[key] = value
    if values.get('smtp_security') not in (None, 'SSL/TLS', 'STARTTLS'):
        raise ValueError('SMTP 加密方式无效')
    if values.get('summary_media_scope') not in (None, '全部媒体', '重点媒体'):
        raise ValueError('按需总结的图片范围无效')
    if values.get('webchat_qq_bot_id') and not values['webchat_qq_bot_id'].isascii():
        raise ValueError('机器人 QQ 号应使用数字')
    if values.get('webchat_qq_bot_id') and not values['webchat_qq_bot_id'].isdigit():
        raise ValueError('机器人 QQ 号应使用数字')
    if values.get('smtp_sender') and not re.fullmatch(
            r'[^\s@]+@[^\s@]+\.[^\s@]+', values['smtp_sender']):
        raise ValueError('发件邮箱应为有效邮箱地址')
    secret = payload.get('smtp_password')
    clear = payload.get('smtp_password_clear', False)
    if not isinstance(clear, bool):
        raise ValueError('清除授权码选项无效')
    if clear and secret:
        raise ValueError('不能同时填写并清除授权码')
    if secret is not None:
        if not isinstance(secret, str) or len(secret) > 255 or any(ord(ch) < 32 for ch in secret):
            raise ValueError('SMTP 授权码格式错误')
        if secret:
            values['smtp_password'] = secret
    if clear:
        values['smtp_password'] = ''
    return values
