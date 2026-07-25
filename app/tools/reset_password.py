#!/usr/bin/env python3
"""Reset a DockerOps SQLite user password from the host (via docker exec).

Examples (on Unraid / NAS shell):

  docker exec -it DockerOps python -m tools.reset_password \\
    --username DeltrivX --password 'your-new-password'

  # interactive password (hidden):
  docker exec -it DockerOps python -m tools.reset_password --username DeltrivX

  # list users only:
  docker exec -it DockerOps python -m tools.reset_password --list
"""
from __future__ import annotations

import argparse
import getpass
import sys

# Ensure /app is on path when run as module inside container
from auth import hash_password, validate_password, validate_username
from db import (
    audit,
    create_user,
    delete_all_users_and_sessions,
    init_db,
    list_usernames,
    update_user_password,
)
from fastapi import HTTPException


def _list() -> int:
    init_db()
    names = list_usernames()
    if not names:
        print("（无用户）SQLite users 表为空 — 打开 Web 将强制首次初始化")
        return 0
    print("已有账号：")
    for n in names:
        print(f"  - {n}")
    return 0


def _reset(username: str, password: str, create_if_missing: bool) -> int:
    init_db()
    try:
        username = validate_username(username)
        password = validate_password(password)
    except HTTPException as e:
        print(f"错误: {e.detail}", file=sys.stderr)
        return 2

    names = list_usernames()
    if username not in names:
        if not create_if_missing:
            print(
                f"错误: 用户 {username!r} 不存在。"
                f" 现有: {', '.join(names) or '（无）'}。"
                f" 若要新建可加 --create",
                file=sys.stderr,
            )
            return 1
        create_user(username, hash_password(password), role="admin")
        audit("password_reset_cli_create", actor="cli", detail={"username": username})
        print(f"已创建用户 {username} 并设置密码（SQLite）")
        return 0

    ok = update_user_password(username, hash_password(password))
    if not ok:
        print("错误: 更新失败", file=sys.stderr)
        return 1
    # Invalidate sessions by wiping sessions table only — keep users
    from db import connect, _lock  # noqa: PLC0415

    with _lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM sessions")
            conn.commit()
        finally:
            conn.close()
    audit("password_reset_cli", actor="cli", detail={"username": username})
    print(f"已重置 {username} 的密码（SQLite），旧登录会话已失效")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.reset_password",
        description="重置 DockerOps 内置 SQLite 账号密码（主机终端使用）",
    )
    p.add_argument("--list", action="store_true", help="仅列出用户名")
    p.add_argument("--username", "-u", help="用户名")
    p.add_argument("--password", "-p", help="新密码（≥6 位）；省略则交互输入")
    p.add_argument(
        "--create",
        action="store_true",
        help="用户不存在时创建为管理员（慎用）",
    )
    p.add_argument(
        "--wipe-all",
        action="store_true",
        help="危险：清空全部用户与会话，下次打开 Web 强制初始化",
    )
    args = p.parse_args(argv)

    if args.wipe_all:
        init_db()
        n = delete_all_users_and_sessions()
        print(f"已清空 {n} 个用户与全部会话。打开 Web 将强制首次初始化。")
        return 0

    if args.list or (not args.username and not args.password):
        if args.list or not args.username:
            return _list()

    if not args.username:
        print("请指定 --username", file=sys.stderr)
        return 2

    password = args.password
    if not password:
        password = getpass.getpass("新密码: ")
        confirm = getpass.getpass("确认密码: ")
        if password != confirm:
            print("两次密码不一致", file=sys.stderr)
            return 2

    return _reset(args.username, password, create_if_missing=args.create)


if __name__ == "__main__":
    raise SystemExit(main())
