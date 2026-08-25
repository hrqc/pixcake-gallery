# -*- coding: utf-8 -*-
"""SQLite 持久化: 项目 / 照片 / 选片状态 / 交付配置 / 客户选片备注 / 交付订单.
使用 WAL, 每请求独立连接 (check_same_thread=False 不需要, 每连接单线程用).
"""
import os, sqlite3, time, json, secrets

import license

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'gallery.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id           TEXT PRIMARY KEY,     -- "{user}_{album}"
  user         TEXT,
  album_id     TEXT,
  path         TEXT NOT NULL,        -- album 绝对路径
  name         TEXT,                 -- 显示名 (可改, 默认 album_id)
  last_scan    INTEGER,
  photo_count  INTEGER DEFAULT 0,
  sel_count    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS photos(
  key          TEXT PRIMARY KEY,     -- "{project_id}|{photo_id}"
  project_id   TEXT NOT NULL,
  photo_id     TEXT NOT NULL,
  thumb_dir    TEXT,                 -- thumbnail_NN_<hash>
  sort_key     INTEGER,              -- NN
  src_3000     TEXT,
  src_375      TEXT,
  mtime_3000   INTEGER,
  mtime_375    INTEGER,
  src_o_3000   TEXT,
  src_o_375    TEXT,
  mtime_o_3000 INTEGER,
  mtime_o_375  INTEGER,
  on_disk      INTEGER DEFAULT 1,
  selected     INTEGER DEFAULT 0,
  selected_at  INTEGER,
  UNIQUE(project_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_photos_project ON photos(project_id);
CREATE INDEX IF NOT EXISTS idx_photos_sel ON photos(project_id, selected);

-- ---- 交付系统 (客户选片+备注 / 付费下载, 摄影师确认后才可下载) ----
CREATE TABLE IF NOT EXISTS delivery_projects(
  project_id    TEXT PRIMARY KEY,      -- 对应 projects.id
  code          TEXT NOT NULL UNIQUE,  -- 交付码: 客户链接 /d/<code>
  title         TEXT,                  -- 客户看到的分享标题
  price         REAL DEFAULT 0,        -- 单价(元/张), 0 = 免费
  free_count    INTEGER DEFAULT 0,     -- 免费张数 (前 N 张不计费)
  tier_min      INTEGER DEFAULT 0,     -- 阶梯门槛: 计费张数>=此值触发阶梯价 (0=无)
  tier_discount REAL DEFAULT 0,        -- 阶梯减价(元/张): 门槛内单价 = price - tier_discount
  enabled       INTEGER DEFAULT 1,     -- 0 = 暂停交付
  pay_qr_path   TEXT,                  -- 摄影师收款码图片路径 (方案①个人收款码)
  public_base   TEXT DEFAULT '',       -- 公网基础地址 (如 https://db1of18959019.vicp.fun)
  created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS delivery_selections(
  project_id    TEXT NOT NULL,
  session       TEXT NOT NULL,         -- 客户浏览器会话 (uuid)
  photo_id      TEXT NOT NULL,
  selected      INTEGER DEFAULT 0,
  note          TEXT DEFAULT '',
  updated_at    INTEGER,
  PRIMARY KEY(project_id, session, photo_id)
);
CREATE TABLE IF NOT EXISTS delivery_orders(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    TEXT NOT NULL,
  session       TEXT NOT NULL,
  photo_ids     TEXT NOT NULL,         -- JSON 数组 (下单时快照)
  count         INTEGER,
  price         REAL,                  -- 计费单价快照 (已扣免费/阶梯后)
  free_count    INTEGER DEFAULT 0,     -- 免费张数快照
  total         REAL,                  -- 应付快照
  paid_amount   REAL,                  -- 客户填的实付金额
  customer_name TEXT DEFAULT '',
  customer_msg  TEXT DEFAULT '',
  status        TEXT DEFAULT 'submitted',  -- submitted(等摄影师确认) / confirmed(已同意) / downloaded(已下载)
  created_at    INTEGER,
  confirmed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deliv_orders_proj ON delivery_orders(project_id);
CREATE INDEX IF NOT EXISTS idx_deliv_orders_session ON delivery_orders(project_id, session, status);

-- ---- 摄影师租户 (多摄影师隔离站点) ----
CREATE TABLE IF NOT EXISTS photographers(
  id            TEXT PRIMARY KEY,      -- slug: p_XXXXXXXX
  name          TEXT DEFAULT '',       -- 显示名
  contact       TEXT DEFAULT '',       -- 摄影师联系方式
  machine_fp    TEXT,                  -- 激活绑定的机器码
  status        TEXT DEFAULT 'active', -- active/disabled
  admin_contact TEXT DEFAULT '',       -- 平台管理员联系方式 (客户端"联系购买"用)
  created_at    INTEGER,
  updated_at    INTEGER
);

-- ---- 卡密激活 (授权系统, 软件卖给摄影师的许可证) ----
CREATE TABLE IF NOT EXISTS license_keys(
  key           TEXT PRIMARY KEY,
  plan_name     TEXT DEFAULT '',      -- 套餐名 (月卡/季卡/年卡/永久/体验卡/张数卡)
  duration_days INTEGER DEFAULT 0,    -- 0=永久
  duration_hours INTEGER DEFAULT 0,   -- 小时级时长 (体验卡 1 小时等)
  quota         INTEGER DEFAULT 0,    -- 0=不限; >0=精修张数额度
  bind_device   INTEGER DEFAULT 0,    -- 1=激活时绑定机器码
  status        TEXT DEFAULT 'unused',-- unused/active/disabled/expired
  bound_fp      TEXT,                 -- 绑定的机器码
  activated_at  INTEGER,
  expires_at    INTEGER DEFAULT 0,
  quota_used    INTEGER DEFAULT 0,
  created_at    INTEGER,
  remark        TEXT DEFAULT ''
);
"""

def _connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        columns = {row['name'] for row in conn.execute("PRAGMA table_info(photos)")}
        migrations = {
            'src_o_3000': 'TEXT',
            'src_o_375': 'TEXT',
            'mtime_o_3000': 'INTEGER',
            'mtime_o_375': 'INTEGER',
        }
        for name, column_type in migrations.items():
            if name not in columns:
                conn.execute('ALTER TABLE photos ADD COLUMN %s %s' % (name, column_type))
        # 卡密表: 小时级时长补列 (旧库没有 duration_hours)
        try:
            lk_cols = {row['name'] for row in conn.execute("PRAGMA table_info(license_keys)")}
            if 'duration_hours' not in lk_cols:
                conn.execute("ALTER TABLE license_keys ADD COLUMN duration_hours INTEGER DEFAULT 0")
        except Exception:
            pass
        # 交付配置: 免费张数 + 阶梯优惠 (已有库补列)
        dp_cols = {row['name'] for row in conn.execute("PRAGMA table_info(delivery_projects)")}
        for name, column_type in (('free_count', 'INTEGER'), ('tier_min', 'INTEGER'),
                                  ('tier_discount', 'REAL')):
            if name not in dp_cols:
                conn.execute(
                    'ALTER TABLE delivery_projects ADD COLUMN %s %s DEFAULT 0' % (name, column_type))
        # 交付订单: 免费张数快照
        do_cols = {row['name'] for row in conn.execute("PRAGMA table_info(delivery_orders)")}
        if 'free_count' not in do_cols:
            conn.execute('ALTER TABLE delivery_orders ADD COLUMN free_count INTEGER DEFAULT 0')
        # 多租户: 卡密绑定摄影师 + 项目归属
        try:
            lk_cols = {row['name'] for row in conn.execute("PRAGMA table_info(license_keys)")}
            if 'tenant' not in lk_cols:
                conn.execute("ALTER TABLE license_keys ADD COLUMN tenant TEXT")
        except Exception:
            pass
        try:
            pr_cols = {row['name'] for row in conn.execute("PRAGMA table_info(projects)")}
            if 'owner' not in pr_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN owner TEXT")
        except Exception:
            pass
        # 摄影师水印: 预览图水印文字 + 开关 (NULL=用平台默认「贺染」, enabled 默认开)
        try:
            pg_cols = {row['name'] for row in conn.execute("PRAGMA table_info(photographers)")}
            if 'watermark_text' not in pg_cols:
                conn.execute("ALTER TABLE photographers ADD COLUMN watermark_text TEXT")
            if 'watermark_enabled' not in pg_cols:
                conn.execute("ALTER TABLE photographers ADD COLUMN watermark_enabled INTEGER DEFAULT 1")
        except Exception:
            pass
        conn.execute('PRAGMA user_version=4')
        conn.commit()
    finally:
        conn.close()

# ---------------- projects ----------------
def upsert_project(p, owner=None):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO projects(id, user, album_id, path, name, last_scan, owner) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET path=excluded.path, user=excluded.user, "
            "album_id=excluded.album_id, last_scan=excluded.last_scan, owner=COALESCE(projects.owner, excluded.owner)",
            (p['id'], p.get('user'), p.get('album_id'), p['path'],
             p.get('name') or p.get('album_id'), p.get('last_scan'), owner))
        conn.commit()
    finally:
        conn.close()

def get_project(pid):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def project_owner(pid):
    """返回项目归属的摄影师 slug (无则 None)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT owner FROM projects WHERE id=?", (pid,)).fetchone()
        return row['owner'] if row else None
    finally:
        conn.close()

def set_project_owner(pid, owner):
    conn = _connect()
    try:
        conn.execute("UPDATE projects SET owner=? WHERE id=?", (owner, pid))
        conn.commit()
    finally:
        conn.close()

def list_projects(owner=None):
    conn = _connect()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM projects WHERE owner=? ORDER BY last_scan DESC", (owner,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM projects ORDER BY last_scan DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def list_projects_for(owner):
    return list_projects(owner)

def rename_project(pid, name):
    conn = _connect()
    try:
        conn.execute("UPDATE projects SET name=? WHERE id=?", (name, pid))
        conn.commit()
    finally:
        conn.close()

# ---------------- photos ----------------
def sync_photos(pid, photos_on_disk):
    """photos_on_disk: list of dict(key, photo_id, thumb_dir, sort_key,
    E/O 的 3000/375 路径与 mtime). 不存在于磁盘的置 on_disk=0."""
    conn = _connect()
    try:
        now = int(time.time())
        for ph in photos_on_disk:
            conn.execute(
                "INSERT INTO photos(key, project_id, photo_id, thumb_dir, sort_key, "
                "  src_3000, src_375, mtime_3000, mtime_375, "
                "  src_o_3000, src_o_375, mtime_o_3000, mtime_o_375, on_disk) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  thumb_dir=excluded.thumb_dir, sort_key=excluded.sort_key, "
                "  src_3000=excluded.src_3000, src_375=excluded.src_375, "
                "  mtime_3000=excluded.mtime_3000, mtime_375=excluded.mtime_375, "
                "  src_o_3000=excluded.src_o_3000, src_o_375=excluded.src_o_375, "
                "  mtime_o_3000=excluded.mtime_o_3000, mtime_o_375=excluded.mtime_o_375, on_disk=1",
                (ph['key'], pid, ph['photo_id'], ph.get('thumb_dir'), ph.get('sort_key'),
                 ph.get('src_3000'), ph.get('src_375'), ph.get('mtime_3000'), ph.get('mtime_375'),
                 ph.get('src_o_3000'), ph.get('src_o_375'),
                 ph.get('mtime_o_3000'), ph.get('mtime_o_375')))
        conn.execute("UPDATE photos SET on_disk=0 WHERE project_id=? AND key NOT IN (%s)"
                     % ','.join('?' for _ in photos_on_disk),
                     [pid] + [ph['key'] for ph in photos_on_disk]) if photos_on_disk else \
            conn.execute("UPDATE photos SET on_disk=0 WHERE project_id=?", (pid,))
        conn.execute("UPDATE projects SET photo_count=(SELECT COUNT(*) FROM photos WHERE project_id=? AND on_disk=1), "
                     "sel_count=(SELECT COUNT(*) FROM photos WHERE project_id=? AND selected=1 AND on_disk=1), last_scan=? WHERE id=?",
                     (pid, pid, now, pid))
        conn.commit()
    finally:
        conn.close()

def list_photos(pid, selected_only=False):
    conn = _connect()
    try:
        q = "SELECT * FROM photos WHERE project_id=? AND on_disk=1"
        args = [pid]
        if selected_only:
            q += " AND selected=1"
        q += " ORDER BY sort_key, photo_id"
        rows = conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_photo(key):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM photos WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def set_selected(key, sel):
    conn = _connect()
    try:
        now = int(time.time())
        conn.execute("UPDATE photos SET selected=?, selected_at=? WHERE key=?",
                     (1 if sel else 0, now if sel else None, key))
        conn.commit()
    finally:
        conn.close()

def set_selected_bulk(keys, sel):
    conn = _connect()
    try:
        now = int(time.time())
        for k in keys:
            conn.execute("UPDATE photos SET selected=?, selected_at=? WHERE key=?",
                         (1 if sel else 0, now if sel else None, k))
        conn.commit()
    finally:
        conn.close()

def list_all_photos(owner=None):
    """全工作区所有在盘照片 (预热用). owner 指定时只取某摄影师的项目."""
    conn = _connect()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM photos WHERE on_disk=1 AND project_id IN "
                "(SELECT id FROM projects WHERE owner=?) ORDER BY project_id, sort_key, photo_id",
                (owner,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM photos WHERE on_disk=1 ORDER BY project_id, sort_key, photo_id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_selected(pid):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM photos WHERE project_id=? AND selected=1 AND on_disk=1 ORDER BY selected_at, sort_key",
            (pid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

# ================= 交付系统 =================
def _new_code():
    """短交付码: 8 位大写字母数字, 去掉 0/O/1/I 等易混淆字符, 客户链接更简短."""
    alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def ensure_delivery(pid):
    """确保交付配置存在 (缺则建行), 返回该行 dict."""
    row = get_delivery(pid)
    if row:
        return row
    conn = _connect()
    try:
        code = _new_code()
        conn.execute(
            "INSERT INTO delivery_projects(project_id, code, title, price, enabled, pay_qr_path, public_base, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, code, None, 0.0, 1, None, '', int(time.time())))
        conn.commit()
    finally:
        conn.close()
    return get_delivery(pid)

def get_delivery(pid):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM delivery_projects WHERE project_id=?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_delivery_by_code(code):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM delivery_projects WHERE code=? AND enabled=1", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def update_delivery(pid, **kw):
    conn = _connect()
    try:
        cols, vals = [], []
        for k, v in kw.items():
            cols.append('%s=?' % k)
            vals.append(v)
        if cols:
            vals.append(pid)
            conn.execute("UPDATE delivery_projects SET %s WHERE project_id=?" % ','.join(cols), vals)
        conn.commit()
    finally:
        conn.close()

def reset_delivery_code(pid):
    conn = _connect()
    try:
        code = _new_code()
        conn.execute("UPDATE delivery_projects SET code=? WHERE project_id=?", (code, pid))
        conn.commit()
        return code
    finally:
        conn.close()

# ---- 客户选片 + 备注 ----
def save_selections_bulk(project_id, session, items):
    """items: list of {photo_id, selected, note}."""
    conn = _connect()
    try:
        now = int(time.time())
        conn.execute(
            "DELETE FROM delivery_selections WHERE project_id=? AND session=?",
            (project_id, session))
        for it in items:
            conn.execute(
                "INSERT INTO delivery_selections(project_id, session, photo_id, selected, note, updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(project_id, session, photo_id) DO UPDATE SET "
                "selected=excluded.selected, note=excluded.note, updated_at=excluded.updated_at",
                (project_id, session, it['photo_id'], 1 if it.get('selected') else 0, it.get('note') or '', now))
        conn.commit()
    finally:
        conn.close()

def get_selections(project_id, session):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT photo_id, selected, note FROM delivery_selections WHERE project_id=? AND session=?",
            (project_id, session)).fetchall()
        return {r['photo_id']: {'selected': r['selected'], 'note': r['note']} for r in rows}
    finally:
        conn.close()


def delivery_session_exists(project_id, session):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM delivery_selections WHERE project_id=? AND session=? LIMIT 1",
            (project_id, session)).fetchone()
        return row is not None
    finally:
        conn.close()


def count_delivery_sessions_since(project_id, since):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT session) FROM delivery_selections "
            "WHERE project_id=? AND updated_at>=?", (project_id, since)).fetchone()
        return int(row[0])
    finally:
        conn.close()

# ---- 交付订单 ----
def create_order(project_id, session, photo_ids, price, free_count, total, paid_amount, customer_name, customer_msg):
    conn = _connect()
    try:
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO delivery_orders(project_id, session, photo_ids, count, price, free_count, total, "
            "  paid_amount, customer_name, customer_msg, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, session, json.dumps(photo_ids, ensure_ascii=False), len(photo_ids),
             price, free_count, total, paid_amount, customer_name or '', customer_msg or '', 'submitted', now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def count_orders_since(project_id, since):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM delivery_orders WHERE project_id=? AND created_at>=?",
            (project_id, since)).fetchone()
        return int(row[0])
    finally:
        conn.close()

def get_active_order(project_id, session):
    """未完成订单 (submitted/confirmed), 无则 None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM delivery_orders WHERE project_id=? AND session=? "
            "AND status IN ('submitted','confirmed') ORDER BY id DESC LIMIT 1",
            (project_id, session)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_order(order_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM delivery_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def confirm_order(order_id):
    conn = _connect()
    try:
        conn.execute("UPDATE delivery_orders SET status='confirmed', confirmed_at=? WHERE id=? AND status='submitted'",
                     (int(time.time()), order_id))
        conn.commit()
    finally:
        conn.close()

def mark_order_downloaded(order_id):
    conn = _connect()
    try:
        conn.execute("UPDATE delivery_orders SET status='downloaded' WHERE id=?", (order_id,))
        conn.commit()
    finally:
        conn.close()

def list_orders(project_id):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM delivery_orders WHERE project_id=? ORDER BY id DESC",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def delete_order(order_id):
    """作废订单 (摄影师拒绝) — 客户可重新选片."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM delivery_orders WHERE id=?", (order_id,))
        conn.commit()
    finally:
        conn.close()


def count_photos_for_owner(owner):
    """某摄影师所有项目在盘照片总数."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE on_disk=1 AND project_id IN "
            "(SELECT id FROM projects WHERE owner=?)", (owner,)).fetchone()
        return row['c'] if row else 0
    finally:
        conn.close()


def count_orders_for_owner(owner):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM delivery_orders WHERE project_id IN "
            "(SELECT id FROM projects WHERE owner=?)", (owner,)).fetchone()
        return row['c'] if row else 0
    finally:
        conn.close()


def list_all_orders(limit=200):
    """全平台订单总览 (管理端): 每单含归属摄影师 owner 与交付码 code."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT o.*, p.owner, p.name AS project_name, d.code "
            "FROM delivery_orders o "
            "LEFT JOIN projects p ON p.id = o.project_id "
            "LEFT JOIN delivery_projects d ON d.project_id = o.project_id "
            "ORDER BY o.created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def tenant_stats(owner):
    """摄影师站点概况: 相册数/照片数/订单数/待处理订单数."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT p.id) projs, "
            "  (SELECT COUNT(*) FROM photos ph WHERE ph.on_disk=1 AND ph.project_id IN "
            "    (SELECT id FROM projects WHERE owner=?)) photos, "
            "  (SELECT COUNT(*) FROM delivery_orders o WHERE o.project_id IN "
            "    (SELECT id FROM projects WHERE owner=?)) orders, "
            "  (SELECT COUNT(*) FROM delivery_orders o WHERE o.status='submitted' AND o.project_id IN "
            "    (SELECT id FROM projects WHERE owner=?)) pending "
            "FROM projects p WHERE p.owner=?", (owner, owner, owner, owner)).fetchone()
        return {'projects': row['projs'] if row else 0,
                'photos': row['photos'] if row else 0,
                'orders': row['orders'] if row else 0,
                'pending': row['pending'] if row else 0}
    finally:
        conn.close()


# ================= 卡密激活 (授权系统) =================
def create_license_keys(plan_name, duration_days, quota, bind_device, count, remark='',
                        duration_hours=0):
    """批量生成卡密 (保证唯一), 返回 key 列表. duration_hours 支持小时级时长 (体验卡)."""
    now = int(time.time())
    conn = _connect()
    keys = []
    try:
        for _ in range(count):
            k = license.new_key()
            while conn.execute("SELECT 1 FROM license_keys WHERE key=?", (k,)).fetchone():
                k = license.new_key()
            conn.execute(
                "INSERT INTO license_keys(key, plan_name, duration_days, duration_hours, quota, "
                " bind_device, status, created_at, remark) VALUES(?,?,?,?,?,?,?,?,?)",
                (k, plan_name or '', duration_days or 0, duration_hours or 0, quota or 0,
                 1 if bind_device else 0, 'unused', now, remark or ''))
            keys.append(k)
        conn.commit()
    finally:
        conn.close()
    return keys


def get_license(key):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM license_keys WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_licenses(limit=500):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM license_keys ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_license_status(key, status):
    conn = _connect()
    try:
        conn.execute("UPDATE license_keys SET status=? WHERE key=?", (status, key))
        conn.commit()
    finally:
        conn.close()


def delete_license_key(key):
    """硬删卡密. 返回是否删除成功."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM license_keys WHERE key=?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def extend_license(key, days):
    """在现有到期时间上顺延 days 天 (永久卡/未激活卡返回 False)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT expires_at FROM license_keys WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        now = int(time.time())
        base = max(row['expires_at'] or 0, now)
        exp = base + days * 86400
        conn.execute("UPDATE license_keys SET expires_at=?, status='active' WHERE key=?",
                     (exp, key))
        conn.commit()
        return True
    finally:
        conn.close()


def activate_license(key, machine):
    """授权服务器端: 首次激活 / 同机重激活. 返回 (ok, payload_or_error)."""
    row = get_license(key)
    if not row:
        return False, '卡密不存在'
    if row['status'] == 'disabled':
        return False, '卡密已被禁用'
    now = int(time.time())
    if row['status'] == 'active':
        if row['bind_device'] and row['bound_fp'] and row['bound_fp'] != machine:
            return False, '卡密已绑定其他设备'
        if row['expires_at'] and now > row['expires_at']:
            return False, '卡密已过期'
        return True, _license_payload(row, machine)
    exp = now + row['duration_days'] * 86400 + (row['duration_hours'] or 0) * 3600 \
        if (row['duration_days'] or 0) > 0 or (row['duration_hours'] or 0) > 0 else 0
    conn = _connect()
    try:
        conn.execute(
            "UPDATE license_keys SET status='active', bound_fp=?, activated_at=?, expires_at=? "
            "WHERE key=?",
            (machine if row['bind_device'] else None, now, exp, key))
        conn.commit()
    finally:
        conn.close()
    row['status'] = 'active'
    row['bound_fp'] = machine if row['bind_device'] else None
    row['expires_at'] = exp
    return True, _license_payload(row, machine)


def verify_license(key, machine, quota_delta=0):
    """授权服务器端: 在线续期校验 + 张数卡额度累计. 返回 (valid, payload_or_reason)."""
    row = get_license(key)
    if not row:
        return False, 'none'
    if row['status'] == 'disabled':
        return False, 'disabled'
    if row['status'] == 'unused':
        return False, 'unused'
    if row['bind_device'] and row['bound_fp'] and row['bound_fp'] != machine:
        return False, 'device'
    if quota_delta:
        conn = _connect()
        try:
            conn.execute("UPDATE license_keys SET quota_used=quota_used+? WHERE key=?",
                         (quota_delta, key))
            conn.commit()
        finally:
            conn.close()
    row = get_license(key)
    if row['expires_at'] and int(time.time()) > row['expires_at']:
        set_license_status(key, 'expired')
        return False, 'expired'
    if row['quota'] and row['quota_used'] >= row['quota']:
        return False, 'quota'
    return True, _license_payload(row, machine)


def license_stats():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM license_keys GROUP BY status").fetchall()
        by_status = {r['status']: r['c'] for r in rows}
        total = sum(by_status.values())
        return {'total': total, 'by_status': by_status}
    finally:
        conn.close()


def _license_payload(row, machine):
    return {
        'key': row['key'],
        'plan_name': row['plan_name'] or '',
        'expires_at': row['expires_at'] or 0,
        'quota': row['quota'] or 0,
        'quota_used': row['quota_used'] or 0,
        'machine': machine,
    }


# ---------------- 摄影师租户 (多摄影师隔离) ----------------
def create_photographer(name='', machine_fp=''):
    """新建摄影师租户, 返回 dict."""
    slug = 'p_' + secrets.token_hex(4)
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO photographers(id, name, machine_fp, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (slug, (name or '')[:60], machine_fp, 'active', now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM photographers WHERE id=?", (slug,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_photographer(slug):
    if not slug:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM photographers WHERE id=?", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_photographer_by_machine(fp):
    if not fp:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM photographers WHERE machine_fp=? "
                           "ORDER BY updated_at DESC LIMIT 1", (fp,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_photographers():
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM photographers ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_photographer(slug, **kw):
    """更新摄影师字段 (name/contact/admin_contact/machine_fp/watermark_* 等)."""
    allowed = ('name', 'contact', 'admin_contact', 'machine_fp', 'status',
               'watermark_text', 'watermark_enabled')
    fields = {k: v for k, v in kw.items() if k in allowed}
    if not fields or not slug:
        return None
    fields['updated_at'] = int(time.time())
    conn = _connect()
    try:
        conn.execute("UPDATE photographers SET %s WHERE id=?" %
                     ','.join('%s=?' % k for k in fields), list(fields.values()) + [slug])
        conn.commit()
        row = conn.execute("SELECT * FROM photographers WHERE id=?", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_photographer_status(slug, status):
    return update_photographer(slug, status=status)


def bind_card_tenant(key, tenant):
    conn = _connect()
    try:
        conn.execute("UPDATE license_keys SET tenant=? WHERE key=?", (tenant, key))
        conn.commit()
    finally:
        conn.close()


def tenant_active_card(tenant):
    """摄影师当前生效的卡: status=active 且未到期且绑定该租户, 取最近激活的.
    排除已过期但 status 尚未标记的卡 (换新卡后旧卡不再遮蔽新卡);
    同秒激活按 rowid 决胜 (后插入的新卡优先)."""
    if not tenant:
        return None
    conn = _connect()
    try:
        now = int(time.time())
        row = conn.execute(
            "SELECT * FROM license_keys WHERE tenant=? AND status='active' "
            "AND (expires_at = 0 OR expires_at > ?) "
            "ORDER BY COALESCE(activated_at,0) DESC, rowid DESC LIMIT 1", (tenant, now)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def tenant_cards(tenant):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM license_keys WHERE tenant=? ORDER BY COALESCE(activated_at,0) DESC",
            (tenant,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def consume_quota(tenant, count):
    """客户下载无水印图时扣摄影师张数卡额度 (服务器强校验).
    返回 (ok, reason_or_remaining). 无张数限制 (quota=0) 直接通过.
    reason: no_card / expired / disabled / quota 或 remaining 剩余张数."""
    card = tenant_active_card(tenant)
    if not card:
        return False, 'no_card'
    if card['status'] != 'active':
        return False, 'disabled'
    if card['expires_at'] and int(time.time()) > card['expires_at']:
        set_license_status(card['key'], 'expired')
        return False, 'expired'
    if (card['quota'] or 0) > 0:
        used = card['quota_used'] or 0
        if used + count > card['quota']:
            return False, 'quota'
        conn = _connect()
        try:
            conn.execute("UPDATE license_keys SET quota_used=quota_used+? WHERE key=?",
                         (count, card['key']))
            conn.commit()
        finally:
            conn.close()
        return True, card['quota'] - used - count
    return True, -1  # 时间卡/永久卡, 不限额


def check_quota(tenant, count):
    """导出前预检: 只判断能否扣 count 张, 不实际扣减.
    返回 (ok, reason_or_remaining). 无张数限制 (quota=0) 直接通过."""
    card = tenant_active_card(tenant)
    if not card:
        return False, 'no_card'
    if card['status'] != 'active':
        return False, 'disabled'
    if card['expires_at'] and int(time.time()) > card['expires_at']:
        set_license_status(card['key'], 'expired')
        return False, 'expired'
    if (card['quota'] or 0) > 0:
        remaining = card['quota'] - (card['quota_used'] or 0)
        if remaining < count:
            return False, 'quota'
        return True, remaining - count
    return True, -1


def add_quota_to_key(key, n):
    """管理员给卡加张数 (n>0 增, n<0 减)."""
    n = int(n or 0)
    if not key or n == 0:
        return None
    conn = _connect()
    try:
        conn.execute("UPDATE license_keys SET quota=quota+? WHERE key=?", (n, key))
        conn.commit()
        row = conn.execute("SELECT * FROM license_keys WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
