# -*- coding: utf-8 -*-
"""在 JPEG 预览图上叠加斜纹文字水印 (防客户右键/长按白拿无水印精修图).

下载接口仍返回无水印原图; 本模块只负责"未授权预览"的水印渲染.
水印样式参考像素蛋糕: 45° 斜铺、半透明灰、斜向网格间隔. 字号按图片长边自适应
(不写死 300px), 保持可辨识又不遮挡画面 —— 影响观看就没人愿意选了.
渲染失败时静默返回原图 (水印缺失不阻断选片), 错误记 warning 日志.
"""

from __future__ import annotations

import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger('overlay')

HERE = os.path.dirname(os.path.abspath(__file__))

# 字体候选: 打包资源优先, 本机/系统兜底 (均开源可分发或仅开发机回退)
FONT_CANDIDATES = (
    os.path.join(HERE, 'watermark_assets', 'fonts', 'NotoSansCJKsc-Bold.otf'),
    os.path.join(HERE, 'watermark_assets', 'fonts', 'NotoSansSC-Bold.otf'),
    os.path.join(HERE, 'watermark_assets', 'fonts', 'NotoSansSC-Regular.otf'),
    'C:/Windows/Fonts/msyhbd.ttc',          # 开发机 (微软雅黑粗体)
    'C:/Windows/Fonts/msyh.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',   # Linux 兜底
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
)

# 水印视觉参数 (保持"可见但不上画面"): 字号=长边×RATIO, 淡灰半透明, 斜向网格.
FONT_RATIO = 0.07       # 3000px 长边 → 210px; 375 缩略图 → 26px
OPACITY = 50            # 0-255, 50 ≈ 20% 不透明度
COLOR = (140, 140, 140)  # 中性灰
ANGLE = 45
ROW_GAP = 1.5           # 行距 = 瓦片高 × 1.5
COL_GAP = 2.2           # 列距 = 瓦片宽 × 2.2 (像素蛋糕式间隔)
ROW_OFFSET = 0.5        # 每行水平错开 0.5 瓦片宽 (斜向错位)

_BASE_FONT = None


def _find_font():
    for path in FONT_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def _get_font(size):
    """按需字号取字体; 基字面加载一次, font_variant 复用."""
    global _BASE_FONT
    if _BASE_FONT is None:
        path = _find_font()
        if not path:
            log.warning('overlay: 未找到可用中文字体 (candidates=%r)', FONT_CANDIDATES)
            return None
        try:
            _BASE_FONT = ImageFont.truetype(path, 100)
        except Exception as exc:
            log.warning('overlay: 字体加载失败 %s: %s', path, exc)
            return None
    try:
        return _BASE_FONT.font_variant(size=int(size))
    except Exception:
        return None


def _make_tile(font, size, text):
    """画单个水印词 → 旋转角度 → 返回 RGBA 瓦片."""
    draw_probe = ImageDraw.Draw(Image.new('RGBA', (8, 8), (0, 0, 0, 0)))
    bbox = draw_probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(6, int(size * 0.06))
    tile = Image.new('RGBA', (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.text(
        (pad - bbox[0], pad - bbox[1]), text, font=font,
        fill=COLOR + (OPACITY,),
    )
    return tile.rotate(ANGLE, expand=True, resample=Image.Resampling.BICUBIC)


def _tile_layer(width, height, tile):
    layer = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    tw, th = tile.size
    step_x = max(tw + 4, int(tw * COL_GAP))
    step_y = max(th + 4, int(th * ROW_GAP))
    offset = int(tw * ROW_OFFSET)
    row = 0
    y = -th
    while y < height:
        x = -(offset if row % 2 else 0)
        while x < width:
            layer.paste(tile, (x, y), tile)
            x += step_x
        y += step_y
        row += 1
    return layer


def _encode_jpeg(image, source_info):
    output = io.BytesIO()
    options = {'format': 'JPEG', 'quality': 94, 'subsampling': 0}
    for key in ('exif', 'icc_profile', 'dpi'):
        if source_info.get(key) is not None:
            options[key] = source_info[key]
    image.save(output, **options)
    return output.getvalue()


def apply_overlay(jpeg, text='贺染', font_path=None, font_size=None,
                  opacity=OPACITY, color=COLOR, angle=ANGLE):
    """在 JPEG 上叠加斜纹水印, 返回新 JPEG bytes. 任何失败返回原 bytes.

    text      水印词 (摄影师可自定义, 默认「贺染」)
    font_size 默认按图片长边 × FONT_RATIO 自适应; 显式传则用该值
    """
    try:
        with Image.open(io.BytesIO(jpeg)) as source:
            rgb = source.convert('RGB')
            info = dict(source.info)
            width, height = rgb.size
        if font_size is None:
            font_size = max(16, int(round(max(width, height) * FONT_RATIO)))
        font = _get_font(font_size)
        if font is None:
            return jpeg
        tile = _make_tile(font, font_size, text or '贺染')
        layer = _tile_layer(width, height, tile)
        out = Image.alpha_composite(rgb.convert('RGBA'), layer).convert('RGB')
        return _encode_jpeg(out, info)
    except Exception as exc:
        log.warning('overlay: 水印渲染失败, 返回原图: %s', exc)
        return jpeg
