"""Send a complete scheduled report over authenticated, encrypted SMTP."""
import re
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from urllib.parse import urlsplit


SECTIONS = ('重要讨论', '有用资源', '你的待办与提醒', '待确认的信息')
URL_RE = re.compile(r'https?://[^\s<>\[\]]+')


def linked_text(value):
    """Escape untrusted group text and link only explicit HTTP(S) URLs."""
    source = re.sub(r'\[[^\]]+\]\((https?://[^\s)]+)\)', r'\1', str(value))
    parts = []
    cursor = 0
    for match in URL_RE.finditer(source):
        parts.append(escape(source[cursor:match.start()]))
        raw = match.group(0)
        url = raw.rstrip('，。；;、）)]')
        suffix = raw[len(url):]
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and bool(parsed.netloc)
        except ValueError:
            valid = False
        if valid:
            safe = escape(url, quote=True)
            parts.append(f'<a href="{safe}" style="color:#1d5fbf;word-break:break-all;">'
                         f'{escape(url)}</a>')
        else:
            parts.append(escape(url))
        parts.append(escape(suffix))
        cursor = match.end()
    parts.append(escape(source[cursor:]))
    return ''.join(parts).replace('\n', '<br>')


def report_sections(body):
    """Split the existing four-column plain report without asking the model again."""
    header = []
    sections = {name: [] for name in SECTIONS}
    footer = []
    current = None
    for line in str(body).replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        label = line.strip()
        if label.startswith('【') and label.endswith('】') and label[1:-1] in sections:
            current = label[1:-1]
        elif label.startswith('覆盖说明：'):
            footer.append(label)
            current = '_footer'
        elif current == '_footer':
            footer.append(line)
        elif current:
            sections[current].append(line)
        else:
            header.append(line)
    return header, sections, footer


def _items(lines):
    items = []
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if value.startswith(('•', '-', '·')):
            items.append([value[1:].strip(), []])
        elif items:
            items[-1][1].append(value)
        else:
            items.append([value, []])
    return items


def render_report_html(body):
    """Email-only layout; the plain report remains the source of truth."""
    header, sections, footer = report_sections(body)
    title = next((line.strip() for line in header if line.strip()), '群聊总结')
    metadata = [line.strip() for line in header[1:] if line.strip()]
    out = [
        '<!doctype html><html lang="zh-CN"><body style="margin:0;padding:24px 12px;'
        'background:#f4f7fb;color:#17212f;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',sans-serif;">',
        '<div style="max-width:720px;margin:0 auto;background:#fff;border:1px solid #e4eaf2;'
        'border-radius:14px;overflow:hidden;">',
        '<div style="padding:30px 30px 24px;background:#edf4ff;border-bottom:1px solid #dbe7f7;">',
        f'<h1 style="margin:0 0 14px;font-size:24px;line-height:1.35;">{linked_text(title)}</h1>',
    ]
    for line in metadata:
        out.append('<div style="color:#506177;font-size:13px;line-height:1.7;">'
                   + linked_text(line) + '</div>')
    out.append('</div><div style="padding:4px 30px 24px;">')
    if not any(sections.values()):
        out.append('</div></div></body></html>')
        return ''.join(out)
    for index, name in enumerate(SECTIONS):
        lines = sections[name]
        out.append('<section style="margin-top:28px;">'
                   f'<h2 style="margin:0 0 14px;padding-bottom:10px;border-bottom:2px solid '
                   f'{"#2768c7" if index < 2 else "#dce5f0"};font-size:18px;">'
                   f'{escape(name)}</h2>')
        items = _items(lines)
        if not items or (len(items) == 1 and items[0][0].startswith('无')
                         and not items[0][1]):
            out.append('<p style="margin:0;color:#66748a;font-size:14px;">无</p>')
        elif name == '重要讨论':
            for heading, details in items:
                out.append('<div style="margin:0 0 14px;padding:16px 18px;border-left:4px solid '
                           '#2768c7;background:#f7faff;border-radius:0 9px 9px 0;">'
                           f'<h3 style="margin:0 0 8px;font-size:16px;line-height:1.5;">'
                           f'{linked_text(heading)}</h3>')
                if details:
                    out.append('<p style="margin:0;font-size:14px;line-height:1.75;">'
                               + linked_text('\n'.join(details)) + '</p>')
                out.append('</div>')
        elif name == '有用资源':
            for heading, details in items:
                title_part, sep, rest = heading.partition('：')
                resource_name = title_part if sep else heading
                description = ([rest] if sep and rest else []) + details
                out.append('<div style="padding:14px 0;border-bottom:1px solid #e7edf5;">'
                           f'<div style="font-size:15px;font-weight:700;line-height:1.5;">'
                           f'{linked_text(resource_name)}</div>')
                if description:
                    out.append('<div style="margin-top:6px;font-size:14px;line-height:1.7;'
                               'overflow-wrap:anywhere;">'
                               + linked_text('\n'.join(description)) + '</div>')
                out.append('</div>')
        else:
            for heading, details in items:
                content = '\n'.join([heading, *details])
                out.append('<p style="margin:0 0 11px;font-size:14px;line-height:1.7;">'
                           + linked_text(content) + '</p>')
        out.append('</section>')
    if footer:
        out.append('<div style="margin-top:24px;padding:14px 16px;background:#f5f7fa;'
                   'color:#607086;border-radius:8px;font-size:12px;line-height:1.6;">'
                   + linked_text('\n'.join(footer)) + '</div>')
    out.append('</div></div></body></html>')
    return ''.join(out)


def email_address(value):
    address = str(value or '').strip()
    if (len(address) > 254 or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            address)):
        raise ValueError('请输入一个有效的邮箱地址')
    return address


def smtp_options(config):
    host = str(config.get('smtp_host', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('请先在插件设置中填写 SMTP 服务器地址')
    try:
        port = int(config.get('smtp_port', 465))
    except (TypeError, ValueError) as exc:
        raise ValueError('SMTP 端口无效') from exc
    if not 1 <= port <= 65535:
        raise ValueError('SMTP 端口无效')
    security = str(config.get('smtp_security', 'SSL/TLS')).strip()
    if security not in ('SSL/TLS', 'STARTTLS'):
        raise ValueError('SMTP 加密方式应为 SSL/TLS 或 STARTTLS')
    username = str(config.get('smtp_username', '')).strip()
    password = str(config.get('smtp_password', '') or '')
    if not username or not password:
        raise ValueError('请先在插件设置中填写 SMTP 用户名和授权码')
    sender = email_address(config.get('smtp_sender') or username)
    if host.lower() == 'smtp.qq.com' and sender.lower() != username.lower():
        raise ValueError('QQ 邮箱的发件邮箱须与 SMTP 用户名相同；可将发件邮箱留空')
    return host, port, security, username, password, sender


def send_email(config, recipient, subject, body):
    host, port, security, username, password, sender = smtp_options(config)
    recipient = email_address(recipient)
    message = EmailMessage()
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = str(subject).replace('\r', ' ').replace('\n', ' ')[:180]
    message.set_content(str(body))
    message.add_alternative(render_report_html(body), subtype='html')
    tls = ssl.create_default_context()
    if security == 'SSL/TLS':
        with smtplib.SMTP_SSL(host, port, timeout=20, context=tls) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls)
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def smtp_error_detail(exc):
    """Return a useful category without revealing SMTP credentials or report text."""
    if isinstance(exc, ValueError) and str(exc) in (
            '请输入一个有效的邮箱地址',
            '请先在插件设置中填写 SMTP 服务器地址',
            'SMTP 端口无效',
            'SMTP 加密方式应为 SSL/TLS 或 STARTTLS',
            '请先在插件设置中填写 SMTP 用户名和授权码',
            'QQ 邮箱的发件邮箱须与 SMTP 用户名相同；可将发件邮箱留空'):
        return str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'SMTP 认证失败，请检查授权码'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ('SMTP 拒绝发件地址（代码 ' + str(exc.smtp_code)
                + '）；请核对发件邮箱与 SMTP 用户名是否一致')
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'SMTP 拒绝收件地址'
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return 'SMTP 服务器不支持 STARTTLS'
    if isinstance(exc, smtplib.SMTPResponseException):
        return 'SMTP 拒绝发送（代码 ' + str(exc.smtp_code) + '）'
    if isinstance(exc, (TimeoutError, OSError)):
        return 'SMTP 连接或发送失败（' + type(exc).__name__ + '）'
    return type(exc).__name__
