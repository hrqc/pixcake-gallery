# -*- coding: utf-8 -*-
"""扫描像素蛋糕工作区, 配对精修照片 (c_p_f_e) 与原图 (c_p_f_o)."""
import os, re, time

# 像素蛋糕工作区. 结构: <ws>/<user>/<album>/thumbnail_cache/<thumb>/c_p_f_e/<ID>_3000
DEFAULT_WS = r'D:/xsdg/像素蛋糕/.PixCake-qt_pro Workspace/project'

_thumb_num_re = re.compile(r'thumbnail_(\d+)_')


def _thumb_sort_key(td):
    m = _thumb_num_re.match(td)
    return int(m.group(1)) if m else 1 << 30


def find_albums(ws_root):
    """返回 [{id, user, album_id, path}], 按 (user, album) 排序."""
    albums = []
    if not os.path.isdir(ws_root):
        return albums
    for user in sorted(os.listdir(ws_root)):
        up = os.path.join(ws_root, user)
        if not os.path.isdir(up):
            continue
        for alb in sorted(os.listdir(up)):
            ap = os.path.join(up, alb)
            if os.path.isdir(os.path.join(ap, 'thumbnail_cache')):
                albums.append({'id': '%s_%s' % (user, alb), 'user': user,
                               'album_id': alb, 'path': ap})
    return albums


def scan_project_photos(album_path):
    """扫描一个相册目录, 返回磁盘上的精修照片列表.
    每个照片同时包含精修图和原图的 3000/375 路径与修改时间。"""
    photos = []
    tc = os.path.join(album_path, 'thumbnail_cache')
    if not os.path.isdir(tc):
        return photos
    for td in sorted(os.listdir(tc), key=_thumb_sort_key):
        cfe = os.path.join(tc, td, 'c_p_f_e')
        cfo = os.path.join(tc, td, 'c_p_f_o')
        if not os.path.isdir(cfe):
            continue
        for f in os.listdir(cfe):
            # 只关心精修预览 <ID>_3000 (排除 json/_ext/info)
            if not f.endswith('_3000'):
                continue
            base = f[:-5]  # 去掉 _3000
            if base.endswith('.json') or base.endswith('_ext') or base == 'info':
                continue
            p3000 = os.path.join(cfe, f)
            p375 = os.path.join(cfe, base + '_375')
            if not os.path.isfile(p375):
                p375 = None
            po3000 = os.path.join(cfo, f)
            if not os.path.isfile(po3000):
                po3000 = None
            po375 = os.path.join(cfo, base + '_375')
            if not os.path.isfile(po375):
                po375 = None
            try:
                m3000 = int(os.path.getmtime(p3000))
            except OSError:
                continue
            m375 = int(os.path.getmtime(p375)) if p375 else 0
            mo3000 = int(os.path.getmtime(po3000)) if po3000 else 0
            mo375 = int(os.path.getmtime(po375)) if po375 else 0
            photos.append({
                'key': base, 'photo_id': base, 'thumb_dir': td,
                'sort_key': _thumb_sort_key(td),
                'src_3000': p3000, 'src_375': p375,
                'mtime_3000': m3000, 'mtime_375': m375,
                'src_o_3000': po3000, 'src_o_375': po375,
                'mtime_o_3000': mo3000, 'mtime_o_375': mo375,
            })
    return photos
