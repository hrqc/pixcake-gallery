# -*- coding: utf-8 -*-
"""管理端客户端入口: 本地工作台 (127.0.0.1) + 平台管理控制台.
打包后双击即用: 自动开浏览器, 数据目录 ~/.pixcake-admin, 免登录 (仅本机可达).
"""
import os
import sys
import threading
import time
import webbrowser


def main():
    import argparse
    ap = argparse.ArgumentParser(description='像素蛋糕 · 管理端 (本地工作台 + 平台管理)')
    ap.add_argument('--port', type=int, default=8890)
    ap.add_argument('--ws', default=None, help='像素蛋糕工作区 (默认内置探测路径)')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()

    data_dir = os.path.join(os.path.expanduser('~'), '.pixcake-admin')
    os.makedirs(data_dir, exist_ok=True)

    import gallery
    argv = ['gallery.py', '--port', str(args.port), '--host', '127.0.0.1',
            '--data-dir', data_dir, '--wm-workers', '2', '--no-auth']
    if args.ws:
        argv += ['--ws', args.ws]
    sys.argv = argv

    if not args.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open('http://127.0.0.1:%d/' % args.port)
        threading.Thread(target=_open, daemon=True).start()

    gallery.main()


if __name__ == '__main__':
    main()
