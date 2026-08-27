# -*- mode: python ; coding: utf-8 -*-
"""管理端客户端打包 spec (PyInstaller, Win/Mac 通用).
用法: pyinstaller pixcake-admin.spec  (或 pyinstaller -y pixcake-admin.spec)
"""
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ['launch_admin.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('static', 'static'),
        ('watermark_assets', 'watermark_assets'),
    ],
    hiddenimports=['openpyxl'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pixcake-admin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    icon='app.ico',
    console=True,          # 控制台窗口 = 关闭窗口即退出客户端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='pixcake-admin',
)
