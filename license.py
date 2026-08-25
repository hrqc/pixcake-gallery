# -*- coding: utf-8 -*-
"""卡密激活系统 - 纯工具模块 (无 DB 依赖; db.py 单向依赖本模块).
- 卡密格式: XXXX-XXXX-XXXX-XXXX (去 0/O/1/I 易混淆字符)
- 机器码: MAC + 主机名 hash (客户端软件部署在哪台机器, 就绑定哪台)
- 本地授权文件: data/license.json (客户端持有, HMAC 签名防篡改)
"""
import os
import re
import time
import json
import hmac
import hashlib
import socket
import uuid
import secrets

ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'  # 31 字符, 去 0/O/1/I
KEY_RE = re.compile(r'^[%s]{4}-[%s]{4}-[%s]{4}-[%s]{4}$'
                    % (ALPHABET, ALPHABET, ALPHABET, ALPHABET))
_SALT = b'pixcake-license-v1'  # 签名盐, 做软件阶段再加固


def new_key():
    body = ''.join(secrets.choice(ALPHABET) for _ in range(16))
    return '%s-%s-%s-%s' % (body[0:4], body[4:8], body[8:12], body[12:16])


def valid_key(key):
    return bool(key and KEY_RE.match(key.strip().upper()))


def machine_fp():
    raw = '%s|%s|pixcake' % (uuid.getnode(), socket.gethostname())
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


# ---- 本地授权文件 (客户端软件) ----
def _license_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'data', 'license.json')


def _sign(info):
    raw = json.dumps(
        {k: info.get(k) for k in ('key', 'machine', 'expires_at', 'quota', 'quota_used')
         if k in info},
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hmac.new(_SALT, raw, hashlib.sha256).hexdigest()[:32]


def save_local(info):
    info = dict(info)
    info['sig'] = _sign(info)
    path = _license_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_local():
    path = _license_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def local_status():
    """返回 dict: {state, info}. state: ok / none / tampered / device / expired / quota / blocked.
    blocked 是服务器在线校验的裁决缓存; 若原因本身是到期/超额, 应显示对应状态而不是'禁用'."""
    info = load_local()
    if not info:
        return {'state': 'none', 'info': None}
    if info.get('sig') != _sign(info):
        return {'state': 'tampered', 'info': info}
    if info.get('machine') and info['machine'] != machine_fp():
        return {'state': 'device', 'info': info}
    exp = info.get('expires_at') or 0
    quota = info.get('quota') or 0
    used = info.get('quota_used') or 0
    reason = info.get('blocked_reason') or ''
    if info.get('blocked_at'):
        if reason == 'expired' or (exp and time.time() > exp):
            return {'state': 'expired', 'info': info}
        if reason == 'quota' or (quota and used >= quota):
            return {'state': 'quota', 'info': info}
        return {'state': 'blocked', 'info': info}
    if exp and time.time() > exp:
        return {'state': 'expired', 'info': info}
    if quota and used >= quota:
        return {'state': 'quota', 'info': info}
    return {'state': 'ok', 'info': info}
