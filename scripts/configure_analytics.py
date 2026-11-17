"""Interactive ECS setup: update only the two analytics credentials after read-only checks."""
from __future__ import annotations

import getpass
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, build_opener

TARGET = Path("/opt/diaoshuai")
BACKUPS = Path("/opt/diaoshuai-config-backups")
KEYS = {"ANALYTICS_PASSWORD", "ANALYTICS_DATABASE_URL"}
CONNECTION_KEYS = {
    "DATABASE_URL", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD",
    "MYSQL_DATABASE", "MYSQL_SSL", "SITE_PASSWORD", *KEYS,
}
READ_PRIVILEGES = {"USAGE", "SELECT", "SHOW VIEW", "REPLICATION CLIENT", "REPLICATION SLAVE"}


class SetupError(Exception):
    pass


def database_url(settings: dict, username: str, password: str) -> str:
    host = settings["host"]
    if ":" in host:
        host = f"[{host}]"
    query = {"charset": settings.get("charset") or "utf8mb4"}
    if settings.get("ssl"):
        query["ssl"] = "true"
    return (f"mysql://{quote(username, safe='')}:{quote(password, safe='')}@"
            f"{host}:{int(settings['port'])}/{quote(settings['database'], safe='')}?{urlencode(query)}")


def validate_grants(grants: list[str]) -> None:
    # Do not print SHOW GRANTS: older MySQL versions can include authentication hashes.
    if not grants:
        raise SetupError("无法确认账号权限，请在 RDS 账号管理中检查授权。")
    for grant in grants:
        match = re.match(r"^GRANT\s+(.+?)\s+ON\s+", grant, re.I)
        if not match or re.search(r"\bWITH\s+GRANT\s+OPTION\b", grant, re.I):
            raise SetupError("账号包含角色或转授权权限，无法确认只读；请使用新建的普通只读账号。")
        privileges = {p.strip().upper() for p in match.group(1).split(",")}
        if not privileges <= READ_PRIVILEGES:
            raise SetupError("这个数据库账号包含非只读权限，请在 RDS 中改为普通账号、只读授权。")


def validate_connection(settings: dict, connect) -> None:
    conn = None
    try:
        conn = connect(settings)
        validate_grants([row[0] for row in conn.execute("SHOW GRANTS").fetchall()])
        conn.rollback()
        conn.begin(read_only=True)
        for table in ("records", "style_variants", "style_order_lines", "style_sales"):
            conn.execute(f"SELECT 1 FROM `{table}` LIMIT 1").fetchall()
        conn.rollback()
    except SetupError:
        raise
    except Exception:
        raise SetupError("只读连接测试失败：请核对 RDS 账号、密码及对现有业务库的只读授权；配置尚未修改。") from None
    finally:
        if conn is not None:
            conn.close()


def render_env(original: bytes, updates: dict[str, str]) -> bytes:
    from dotenv import dotenv_values
    from dotenv.parser import parse_stream

    if set(updates) != KEYS:
        raise SetupError("只允许修改两项分析配置。")
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    replacements = {}
    for key, value in updates.items():
        if any(c in value for c in ("\r", "\n", "\0")) or "${" in value:
            raise SetupError("密码不能包含换行、空字符或 ${ 组合。")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        replacements[key] = f"{key}='{escaped}'{newline}"
    seen, chunks = set(), []
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise SetupError("现有 .env 有格式错误，请先检查；不会输出其内容。")
        if binding.key in KEYS:
            if binding.key not in seen:
                chunks.append(replacements[binding.key])
                seen.add(binding.key)
        else:
            chunks.append(binding.original.string)
    result = "".join(chunks)
    for key in sorted(KEYS - seen):
        if result and not result.endswith("\n"):
            result += newline
        result += replacements[key]
    before = dotenv_values(stream=io.StringIO(text), interpolate=False)
    after = dotenv_values(stream=io.StringIO(result), interpolate=False)
    if {k: v for k, v in before.items() if k not in KEYS} != {k: v for k, v in after.items() if k not in KEYS}:
        raise SetupError("其他配置校验失败，已取消写入。")
    if any(after.get(k) != v for k, v in updates.items()):
        raise SetupError("分析配置校验失败，已取消写入。")
    return result.encode("utf-8")


def replace_env(path: Path, original: bytes, updated: bytes, metadata) -> None:
    fd, name = tempfile.mkstemp(prefix=".analytics-env-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or (current.st_ino, current.st_dev) != (metadata.st_ino, metadata.st_dev) or path.read_bytes() != original:
            raise SetupError(".env 在操作期间发生变化，已取消写入，请重新运行。")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def service_properties() -> dict[str, str]:
    result = subprocess.run([
        "systemctl", "show", "diaoshuai", "-p", "MainPID", "-p", "WorkingDirectory",
        "-p", "User", "-p", "ActiveState", "--no-pager",
    ], capture_output=True, text=True, check=True, timeout=15)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def service_environment(properties: dict) -> dict[str, str]:
    pid = properties.get("MainPID", "0")
    if not pid.isdigit() or pid == "0":
        raise SetupError("无法读取当前服务环境。")
    runtime = {}
    for entry in (Path("/proc") / pid / "environ").read_bytes().split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            if key.decode() in CONNECTION_KEYS:
                runtime[key.decode()] = value.decode()
    return runtime


def restart_and_check() -> None:
    subprocess.run(["systemctl", "restart", "diaoshuai"], capture_output=True, check=True, timeout=45)
    client = build_opener(ProxyHandler({}))
    for _ in range(20):
        try:
            with client.open("http://127.0.0.1:8520/api/health", timeout=3) as response:
                state = json.load(response)
            if state.get("ok") and state.get("database") == "mysql" and state.get("storage") == "oss":
                return
        except Exception:
            pass
        time.sleep(1)
    raise SetupError("服务健康检查未通过。")


def ask_secret(label: str) -> str:
    value = getpass.getpass(label)
    if value != getpass.getpass("再输一次确认："):
        raise SetupError("两次输入不一致，配置尚未修改。")
    return value


def main() -> None:
    if os.geteuid() != 0 or sys.platform != "linux" or not sys.stdin.isatty():
        raise SetupError("请在 ECS 的 root 交互终端运行。")
    env_path = TARGET / ".env"
    if TARGET.resolve() != TARGET or env_path.is_symlink() or not env_path.is_file():
        raise SetupError("部署目录或 .env 路径不符合预期。")
    properties = service_properties()
    if properties.get("WorkingDirectory") != str(TARGET) or properties.get("User") != "diaoshuai" or properties.get("ActiveState") != "active":
        raise SetupError("现有 diaoshuai 服务状态、路径或用户不符合预期。")
    metadata, original = env_path.stat(), env_path.read_bytes()
    from dotenv import dotenv_values
    values = dotenv_values(stream=io.StringIO(original.decode("utf-8")))
    runtime = service_environment(properties)
    for key in CONNECTION_KEYS:
        os.environ.pop(key, None)
        if values.get(key) is not None:
            os.environ[key] = values[key]
    os.environ.update(runtime)
    sys.path.insert(0, str(TARGET))
    from src.db import MySQLConnection, mysql_config
    settings = mysql_config()
    if not settings:
        raise SetupError("当前项目没有使用 RDS MySQL，已停止；不会改用 SQLite。")
    print("将为现有 RDS 配置分析只读账号。只更新分析密码和分析连接，其他配置与数据保留。")
    password = ask_secret("输入分析入口密码（输入时不显示）：")
    if not 10 <= len(password) <= 256 or password != password.strip() or password == os.environ.get("SITE_PASSWORD", "").strip():
        raise SetupError("分析密码需 10–256 字符、首尾无空格，且与网站登录密码不同。")
    username = input("RDS 新建的只读账号 [ds_analysis]：").strip() or "ds_analysis"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}[A-Za-z0-9]", username) or username == settings["user"]:
        raise SetupError("请填写独立的 RDS 普通只读账号，不能沿用项目主账号。")
    db_password = ask_secret("输入这个 RDS 只读账号的密码（输入时不显示）：")
    if not db_password:
        raise SetupError("RDS 账号密码不能为空。")
    updates = {"ANALYTICS_PASSWORD": password, "ANALYTICS_DATABASE_URL": database_url(settings, username, db_password)}
    updated = render_env(original, updates)
    print("正在检查只读授权和四张业务表的读取权限……", flush=True)
    validate_connection({**settings, "user": username, "password": db_password}, MySQLConnection)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    if BACKUPS.is_symlink() or BACKUPS.stat().st_uid != 0:
        raise SetupError("配置备份目录不符合预期，已取消写入。")
    backup_dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=BACKUPS))
    backup_file = backup_dir / "env.before"
    with backup_file.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(original)
    replace_env(env_path, original, updated, metadata)
    try:
        print("两项配置已保存，正在重启服务并检查 mysql + oss……", flush=True)
        restart_and_check()
        new_runtime = service_environment(service_properties())
        if any(key in new_runtime and new_runtime[key] != value for key, value in updates.items()):
            raise SetupError("服务环境覆盖了新的分析配置。")
    except BaseException:
        replace_env(env_path, updated, original, env_path.stat())
        try:
            restart_and_check()
        except Exception:
            raise SetupError(f"配置已恢复，但服务仍未通过检查；原配置备份：{backup_file}") from None
        raise SetupError("服务检查未通过，已恢复原配置和服务；请保留当前页面反馈结果。") from None
    print(f"ANALYTICS_CONFIG_OK：只读账号检查通过，两项分析配置已保存；mysql + oss 正常。\n原配置备份：{backup_file}\n接着运行新版部署脚本的检查及更新命令。")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("操作已取消。") from None
    except SetupError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("配置未完成，请检查服务环境和权限；为保护密码，不输出底层错误详情。") from None
