import datetime as dt
import email
import email.header
import os
import re
from collections import OrderedDict
from html import escape as html_escape

import cchardet
import toronado
from lxml import html as lhtml
from lxml.html.clean import Cleaner
from werkzeug.utils import secure_filename

from . import log


def get_charset(name):
    aliases = {
        'unknown-8bit': None,
        'cp-1251': 'cp1251',
        'gb2312': 'gbk',
    }
    return aliases.get(name, name)


def guess_charsets(text, extra=None):
    extra = get_charset(extra)
    detected = cchardet.detect(text)
    detected, confidence = detected['encoding'], detected['confidence']
    if confidence > 0.9 and not extra:
        return [detected]
    charsets = [extra, detected]
    return [c for c in charsets if c]


def decode_str(text, charset, msg_id=None):
    if not text:
        return ''

    def guess_charsets():
        guess = getattr(decode_str, 'guess_charsets')
        if guess:
            return guess()
        return ['utf8']

    charset = get_charset(charset)
    charsets = [charset] if charset else guess_charsets()
    for charset_ in charsets:
        try:
            part = text.decode(charset_)
            break
        except UnicodeDecodeError:
            part = None

    if not part:
        charset_ = charsets[0]
        log.debug('UnicodeDecodeError(%s) -- %s', charset_, msg_id)
        part = text.decode(charset_, 'ignore')
    return part


def decode_header(text, msg_id):
    if not text:
        return ''

    parts_ = email.header.decode_header(text)
    parts = []
    for text, charset in parts_:
        if isinstance(text, str):
            part = text
        else:
            part = decode_str(text, charset, msg_id=msg_id)
        parts += [part]

    header = ''.join(parts)
    header = re.sub('\s+', ' ', header)
    return header


def decode_addresses(text, msg_id):
    if not isinstance(text, str):
        text = str(text)
    res = []
    for name, addr in email.utils.getaddresses([text]):
        name, addr = (decode_header(r, msg_id) for r in [name, addr])
        if addr:
            res += ['"%s" <%s>' % (name if name else addr.split('@')[0], addr)]
    return res


def decode_date(text, *args):
    tm_array = email.utils.parsedate_tz(text)
    tm = dt.datetime(*tm_array[:6]) - dt.timedelta(seconds=tm_array[-1])
    return tm


def parse_part(part, msg_id, attachments_dir, inner=False):
    content = OrderedDict([
        ('files', []),
        ('attachments', []),
        ('embedded', {}),
        ('html', '')
    ])

    ctype = part.get_content_type()
    mtype = part.get_content_maintype()
    stype = part.get_content_subtype()
    if part.is_multipart():
        for m in part.get_payload():
            child = parse_part(m, msg_id, attachments_dir, True)
            child_html = child.pop('html', '')
            content.setdefault('html', '')
            if stype != 'alternative':
                content['html'] += child_html
            elif child_html:
                content['html'] = child_html
            content['files'] += child.pop('files')
            content.update(child)
    elif mtype == 'multipart':
        text = part.get_payload(decode=True)
        text = decode_str(text, part.get_content_charset(), msg_id=msg_id)
        content['html'] = text
    elif part.get_filename() or mtype == 'image':
        payload = part.get_payload(decode=True)
        attachment = {
            'maintype': mtype,
            'type': ctype,
            'id': part.get('Content-ID'),
            'filename': decode_header(part.get_filename(), msg_id),
            'payload': payload,
            'size': len(payload) if payload else None
        }
        content['files'] += [attachment]
    elif ctype in ['text/html', 'text/plain']:
        text = part.get_payload(decode=True)
        text = decode_str(text, part.get_content_charset(), msg_id=msg_id)
        if ctype == 'text/plain':
            text = text2html(text)
        content['html'] = text
    elif ctype in ('message/rfc822', 'text/calendar'):
        pass
    else:
        log.warn('UnknownType(%s) -- %s', ctype, msg_id)

    if inner:
        return content

    content.update(attachments=[], embedded={})
    for index, item in enumerate(content['files']):
        if item['payload']:
            name = secure_filename(item['filename'] or item['id'])
            url = '/'.join([secure_filename(msg_id), str(index), name])
            if item['id'] and item['maintype'] == 'image':
                content['embedded'][item['id']] = url
            elif item['filename']:
                content['attachments'] += [url]
            else:
                log.warn('UnknownAttachment(%s)', msg_id)
                continue
            path = os.path.join(attachments_dir, url)
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'bw') as f:
                    f.write(item['payload'])

    if content['html']:
        htm = re.sub(r'^\s*<\?xml.*?\?>', '', content['html']).strip()
        if not htm:
            content['html'] = htm
            return content

        cleaner = Cleaner(
            links=False,
            safe_attrs_only=False,
            kill_tags=['head'],
            remove_tags=['html', 'body', 'base']
        )
        htm = cleaner.clean_html(htm)
        for cid, path in content['embedded'].items():
            cid = 'cid:%s' % cid.strip('<>')
            path = '/attachments/%s' % path
            htm = re.sub(re.escape(cid), path, htm)

        content['html'] = htm
        if 'text' not in content or not content['text']:
            content['text'] = lhtml.fromstring(htm).text_content()
    return content


def parse(text, msg_id=None, attachments_dir=None):
    attachments_dir = attachments_dir or '/tmp/mailur'

    msg = email.message_from_bytes(text)
    charset = [c for c in msg.get_charsets() if c]
    charset = charset[0] if charset else None
    decode_str.guess_charsets = lambda: guess_charsets(text[:4096], charset)

    decoders = {
        'subject': decode_header,
        'from': decode_addresses,
        'to': decode_addresses,
        'cc': decode_addresses,
        'bcc': decode_addresses,
        'reply-to': decode_addresses,
        'sender': decode_addresses,
        'date': decode_date,
        'message-id': lambda t, *a: str(t),
        'in-reply-to': lambda t, *a: str(t),
        'references': lambda t, *a: str.split(t),
    }
    data = {}
    for key, decode in decoders.items():
        value = msg.get(key)
        data[key] = decode(value, msg_id) if value else None

    msg_id = str(msg_id or data['message-id'])
    files = parse_part(msg, msg_id, attachments_dir)
    data['attachments'] = files['attachments']
    data['embedded'] = files['embedded']
    data['html'] = files.get('html', None)
    data['text'] = files.get('text', None)
    return data


link_regexes = [
    (
        r'(https?://|www\.)[a-z0-9._-]+'
        r'(?:/[/\-_.,a-z0-9%&?;=~#]*)?'
        r'(?:\([/\-_.,a-z0-9%&?;=~#]*\))?'
    ),
    r'mailto:([a-z0-9._-]+@[a-z0-9_._]+[a-z])',
]
link_re = re.compile('(?i)(%s)' % '|'.join(link_regexes))


def text2html(txt):
    txt = txt.strip()
    if not txt:
        return ''

    def fill_link(match):
        return '<a href="{0}" target_="_blank">{0}</a>'.format(match.group())

    htm = html_escape(txt)
    htm = link_re.sub(fill_link, htm)
    htm = '<pre>%s</pre>' % htm
    return htm


def t2h_repl(match):
    groups = match.groupdict()
    blockquote = groups.get('blockquote')
    if blockquote is not None:
        inner = re.sub(r'(?m)^ *> ?', '', blockquote)
        inner = text2html(inner)
        return '<blockquote>%s</blockquote>' % inner
    elif groups.get('p') is not None:
        inner = groups.get('p').strip()
        inner = text2html(inner)
        return '<p>%s</p>' % inner
    elif groups.get('br') is not None:
        return '<br/>'
    else:
        raise ValueError(groups)


def humanize_html(htm, parent=None, class_='email-quote'):
    htm = re.sub(r'(<br[ ]?[/]?>\s*)$', '', htm).strip()
    if htm and parent:
        htm = hide_quote(htm, parent, class_)
    if htm:
        htm = toronado.from_string(htm).decode()
    return htm


def hide_quote(mail1, mail0, class_):
    if not mail0 or not mail1:
        return mail1

    def clean(v):
        v = re.sub('[\s]+', '', v.text_content())
        return v.rstrip()

    t0 = clean(lhtml.fromstring(mail0))
    root1 = lhtml.fromstring(mail1)
    for block in root1.xpath('//blockquote'):
        t1 = clean(block)
        if t0 and t1 and (t0.startswith(t1) or t0.endswith(t1) or t0 in t1):
            block.attrib['class'] = class_
            parent = block.getparent()
            switch = lhtml.fromstring('<div class="%s-switch"/>' % class_)
            block.attrib['class'] = class_
            parent.insert(parent.index(block), switch)
            return lhtml.tostring(root1, encoding='utf8').decode()
    return mail1
