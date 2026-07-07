#!/usr/bin/env python3
"""将一个 task 的视频转换并上传到群晖 File Station（阶段一）。"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import requests


DEFAULT_SOURCE_DIR = Path("/Users/jacky/Documents/Neoteai/sop/20260707")
DEFAULT_DSM_URL = "http://192.168.50.2:5000"
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".webm", ".mpeg", ".mpg"
}


class DSMAPIError(RuntimeError):
    def __init__(self, operation: str, code: Any, response: Any = None) -> None:
        self.operation = operation
        self.code = code
        self.response = response
        super().__init__(f"{operation}失败，DSM error code: {code}")


class DSMClient:
    def __init__(self, base_url: str, timeout: int = 120) -> None:
        self.entry_url = f"{base_url.rstrip('/')}/webapi/entry.cgi"
        self.timeout = timeout
        self.http = requests.Session()
        self.sid: str | None = None

    @staticmethod
    def _decode_response(operation: str, response: requests.Response) -> dict[str, Any]:
        # 不调用 raise_for_status()，避免异常文本把 URL 中的 _sid 打印出来。
        if not response.ok:
            raise RuntimeError(f"{operation} HTTP 请求失败，状态码：{response.status_code}")
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            excerpt = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"{operation}返回的不是 JSON：{excerpt}") from exc
        if not payload.get("success"):
            error = payload.get("error") or {}
            raise DSMAPIError(operation, error.get("code", "未知"), payload)
        return payload

    def login(self, account: str, password: str) -> None:
        params = {
            "api": "SYNO.API.Auth",
            "version": 6,
            "method": "login",
            "account": account,
            "passwd": password,
            "session": "FileStation",
            "format": "sid",
        }
        payload = self._decode_response(
            "登录", self.http.get(self.entry_url, params=params, timeout=self.timeout)
        )
        sid = payload.get("data", {}).get("sid")
        if not sid:
            raise RuntimeError("登录响应成功，但没有返回 sid")
        self.sid = sid
        # Upload 使用 multipart/form-data；同时设置官方支持的 id Cookie，
        # 兼容无法从 multipart 字段识别 _sid 的 DSM 版本。
        self.http.cookies.set("id", sid)

    def logout(self) -> None:
        if not self.sid:
            return
        params = {
            "api": "SYNO.API.Auth",
            "version": 6,
            "method": "logout",
            "session": "FileStation",
            "_sid": self.sid,
        }
        try:
            self._decode_response(
                "登出", self.http.get(self.entry_url, params=params, timeout=self.timeout)
            )
        finally:
            self.sid = None
            self.http.close()

    def _params(self, api: str, method: str, **kwargs: Any) -> dict[str, Any]:
        if not self.sid:
            raise RuntimeError("尚未登录 DSM")
        return {
            "api": api,
            "version": 2,
            "method": method,
            "_sid": self.sid,
            **kwargs,
        }

    def list_folder(self, remote_dir: str) -> list[dict[str, Any]]:
        params = self._params(
            "SYNO.FileStation.List",
            "list",
            folder_path=remote_dir,
            offset=0,
            limit=-1,
            additional=json.dumps(["size"]),
        )
        operation = f"列出远端目录 {remote_dir}"
        for attempt in range(1, 4):
            response = self.http.post(self.entry_url, data=params, timeout=self.timeout)
            if response.status_code not in {502, 503, 504} or attempt == 3:
                payload = self._decode_response(operation, response)
                break
            print(f"[重试] {operation} 收到 HTTP {response.status_code}，第 {attempt}/3 次重试")
            time.sleep(attempt)
        return payload.get("data", {}).get("files", [])

    def create_folder(self, remote_dir: str) -> None:
        path = PurePosixPath(remote_dir)
        params = self._params(
            "SYNO.FileStation.CreateFolder",
            "create",
            folder_path=str(path.parent),
            name=path.name,
            force_parent="true",
        )
        self._decode_response(
            f"创建远端目录 {remote_dir}",
            self.http.post(self.entry_url, data=params, timeout=self.timeout),
        )

    def get_info(self, remote_path: str) -> list[dict[str, Any]]:
        params = self._params(
            "SYNO.FileStation.List",
            "getinfo",
            path=json.dumps([remote_path]),
            additional=json.dumps(["size"]),
        )
        operation = f"查询远端路径 {remote_path}"
        response = self.http.post(self.entry_url, data=params, timeout=self.timeout)
        payload = self._decode_response(operation, response)
        return payload.get("data", {}).get("files", [])

    def ensure_folder_and_list(self, remote_dir: str) -> list[dict[str, Any]]:
        try:
            self.get_info(remote_dir)
        except DSMAPIError as exc:
            # File Station 通用错误码 408：文件或目录不存在。
            if exc.code != 408:
                raise
            print(f"[目录] 目标目录不存在，正在创建：{remote_dir}")
            self.create_folder(remote_dir)
            print("[目录] 创建成功")
        remote_file = f"{remote_dir}/sop_video.mp4"
        try:
            return self.get_info(remote_file)
        except DSMAPIError as exc:
            if exc.code == 408:
                return []
            raise

    def upload(
        self,
        local_file: Path,
        remote_dir: str,
        overwrite: bool,
        remote_name: str = "sop_video.mp4",
        content_type: str = "video/mp4",
    ) -> None:
        params = self._params(
            "SYNO.FileStation.Upload",
            "upload",
            path=remote_dir,
            create_parents="true",
            overwrite=str(overwrite).lower(),
        )
        with local_file.open("rb") as stream:
            files = {"file": (remote_name, stream, content_type)}
            response = self.http.post(
                self.entry_url,
                data=params,
                files=files,
                timeout=self.timeout,
            )
        self._decode_response("上传文件", response)

    def download_text(self, remote_path: str) -> str:
        try:
            info = self.get_info(remote_path)
            if not info:
                return ""
        except DSMAPIError as exc:
            if exc.code == 408:
                return ""
            raise
        params = self._params(
            "SYNO.FileStation.Download", "download", path=remote_path, mode="download"
        )
        # Download 只接受 GET；认证使用登录时设置的 id Cookie，避免 SID 出现在 URL。
        params.pop("_sid", None)
        response = self.http.get(self.entry_url, params=params, timeout=self.timeout)
        if response.status_code == 404:
            return ""
        if not response.ok:
            raise RuntimeError(f"下载 {remote_path} HTTP 请求失败，状态码：{response.status_code}")
        if response.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._decode_response(f"下载 {remote_path}", response)
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"远端日志不是 UTF-8 文本：{remote_path}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="转换并上传一个 SOP 视频到群晖 NAS")
    parser.add_argument("--task-key", help="指定 task 文件夹名；默认按名称排序取第一个")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="本地 task 根目录")
    parser.add_argument("--dsm-url", default=DEFAULT_DSM_URL, help="DSM 地址")
    parser.add_argument("--replace-existing", action="store_true", help="覆盖远端同名文件")
    parser.add_argument("--all", action="store_true", help="处理所有含有效视频的 task，并更新上传日志")
    parser.add_argument("--dry-run", action="store_true", help="只展示操作，不转码、不连接 NAS")
    return parser.parse_args()


def select_task(source_dir: Path, task_key: str | None) -> Path:
    if not source_dir.is_dir():
        raise RuntimeError(f"源目录不存在或不是目录：{source_dir}")
    if task_key:
        if Path(task_key).name != task_key or task_key in {".", ".."}:
            raise RuntimeError("--task-key 必须是直接子文件夹名，不能包含路径")
        task_dir = source_dir / task_key
        if not task_dir.is_dir():
            raise RuntimeError(f"指定的 task 文件夹不存在：{task_dir}")
        return task_dir
    task_dirs = sorted((p for p in source_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not task_dirs:
        raise RuntimeError(f"源目录下没有 task 子文件夹：{source_dir}")
    return task_dirs[0]


def find_video(task_dir: Path) -> Path:
    videos = sorted(
        (p for p in task_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: p.name,
    )
    if not videos:
        raise RuntimeError(f"task 文件夹中没有找到支持的视频文件：{task_dir}")
    if len(videos) > 1:
        names = "、".join(p.name for p in videos)
        raise RuntimeError(f"task 文件夹中发现多个视频，无法确定要上传哪一个：{names}")
    return videos[0]


def transcode(source: Path, output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先安装并确保 ffmpeg 在 PATH 中")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-stats", "-y", "-i", str(source),
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(output),
    ]
    print(f"[转码] 执行：{' '.join(command)}")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg 转码失败，退出码：{exc.returncode}") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("ffmpeg 未生成有效的 sop_video.mp4")


def current_minute() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def upload_task_json(client: DSMClient, task_key: str, remote_dir: str) -> None:
    """生成并覆盖上传 SOP 任务描述文件。task_key 已足够，因此省略 task_id。"""
    payload = {
        "task_key": task_key,
        "nas_path": f"/volume1/database/sop/{task_key}/sop_video.mp4",
        "replace_existing": False,
    }
    with tempfile.TemporaryDirectory(prefix="sop_task_json_") as temp_dir:
        local_json = Path(temp_dir) / "sop_task.json"
        local_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        client.upload(
            local_json,
            remote_dir,
            True,
            remote_name="sop_task.json",
            content_type="application/json; charset=utf-8",
        )
    print(f"[JSON] 已覆盖上传：{remote_dir}/sop_task.json")


def print_progress(completed: int, total: int, status: str = "") -> None:
    width = 32
    ratio = completed / total if total else 1.0
    filled = min(width, int(width * ratio))
    bar = "█" * filled + "░" * (width - filled)
    suffix = f"  {status}" if status else ""
    print(
        f"\r[总进度] |{bar}| {completed}/{total}  {ratio * 100:6.2f}%{suffix}",
        end="",
        flush=True,
    )


def task_dirs_with_videos(source_dir: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    if not source_dir.is_dir():
        raise RuntimeError(f"源目录不存在或不是目录：{source_dir}")
    ready: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    for task_dir in sorted((p for p in source_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        try:
            ready.append((task_dir, find_video(task_dir)))
        except RuntimeError as exc:
            skipped.append(f"{task_dir.name}：{exc}")
    return ready, skipped


def run_all(args: argparse.Namespace) -> int:
    source_dir = args.source_dir.expanduser()
    tasks, invalid_tasks = task_dirs_with_videos(source_dir)
    if not tasks:
        raise RuntimeError("没有找到可以上传的视频")
    print(f"[批量] 找到 {len(tasks)} 个可处理 task，跳过 {len(invalid_tasks)} 个无有效视频的 task")
    for item in invalid_tasks:
        print(f"[跳过] {item}")
    print("[日志] 成功记录格式：YYYY-MM-DD HH:MM<TAB>task_key")
    if args.dry_run:
        for task_dir, source in tasks:
            print(f"[DRY-RUN] {task_dir.name}：{source} -> /database/sop/{task_dir.name}/sop_video.mp4")
            print(
                f"[DRY-RUN] 将生成并覆盖 /database/sop/{task_dir.name}/sop_task.json"
            )
        print("[DRY-RUN] 将下载、追加并覆盖上传 /database/sop/upload_log.txt")
        return 0

    account = input("DSM 账号：").strip()
    password = getpass.getpass("DSM 密码（输入不会显示）：")
    if not account or not password:
        raise RuntimeError("DSM 账号和密码不能为空")

    client = DSMClient(args.dsm_url)
    successes: list[str] = []
    reconciled: list[str] = []
    failures: list[str] = []
    log_path = "/database/sop/upload_log.txt"
    try:
        print(f"[登录] 正在连接：{args.dsm_url}")
        client.login(account, password)
        password = ""
        print("[登录] 成功")
        print(f"[日志] 下载已有日志：{log_path}")
        old_log = client.download_text(log_path)
        log_lines = old_log.splitlines()
        logged_keys = {
            line.rsplit("\t", 1)[-1].strip()
            for line in log_lines
            if line.strip() and "\t" in line
        }
        new_log_lines: list[str] = []
        print_progress(0, len(tasks), "准备开始")

        for index, (task_dir, source) in enumerate(tasks, 1):
            task_key = task_dir.name
            remote_dir = f"/database/sop/{task_key}"
            remote_path = f"{remote_dir}/sop_video.mp4"
            print(f"\n[批量 {index}/{len(tasks)}] task_key={task_key}")
            try:
                entries = client.ensure_folder_and_list(remote_dir)
                exists = any(
                    item.get("name") == "sop_video.mp4" and not item.get("isdir")
                    for item in entries
                )
                if exists and not args.replace_existing:
                    print(f"[跳过上传] 远端已存在：{remote_path}")
                    upload_task_json(client, task_key, remote_dir)
                    if task_key not in logged_keys:
                        new_log_lines.append(f"{current_minute()}\t{task_key}")
                        logged_keys.add(task_key)
                        reconciled.append(task_key)
                        print("[日志] 远端已有文件但日志缺失，本次补记")
                    print_progress(index, len(tasks), f"已存在：{task_key}")
                    continue

                with tempfile.TemporaryDirectory(prefix="upload_sop_") as temp_dir:
                    upload_file = source
                    if source.suffix.lower() != ".mp4":
                        upload_file = Path(temp_dir) / "sop_video.mp4"
                        print(f"[转码] {source.name} -> sop_video.mp4")
                        transcode(source, upload_file)
                    print(f"[上传] {remote_path}（{upload_file.stat().st_size} 字节）")
                    client.upload(upload_file, remote_dir, args.replace_existing)
                upload_task_json(client, task_key, remote_dir)
                successes.append(task_key)
                if task_key not in logged_keys:
                    new_log_lines.append(f"{current_minute()}\t{task_key}")
                    logged_keys.add(task_key)
                print(f"[成功] {task_key}")
                print_progress(index, len(tasks), f"上传成功：{task_key}")
            except (DSMAPIError, requests.RequestException, RuntimeError, OSError) as exc:
                failures.append(task_key)
                print(f"[失败] task_key={task_key}：{exc}", file=sys.stderr)
                print_progress(index, len(tasks), f"失败：{task_key}")

        print()

        if new_log_lines:
            combined = old_log
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += "\n".join(new_log_lines) + "\n"
            with tempfile.TemporaryDirectory(prefix="upload_log_") as temp_dir:
                local_log = Path(temp_dir) / "upload_log.txt"
                local_log.write_text(combined, encoding="utf-8")
                print(f"\n[日志] 追加 {len(new_log_lines)} 条记录并上传：{log_path}")
                client.upload(
                    local_log,
                    "/database/sop",
                    True,
                    remote_name="upload_log.txt",
                    content_type="text/plain; charset=utf-8",
                )
            print("[日志] 上传成功")
        else:
            print("[日志] 没有新记录，无需更新")

        print(
            f"[汇总] 新上传={len(successes)}，补记已有文件={len(reconciled)}，失败={len(failures)}"
        )
        if failures:
            print(f"[失败列表] {', '.join(failures)}", file=sys.stderr)
            return 1
        return 0
    finally:
        if client.sid:
            try:
                client.logout()
                print("[登出] DSM 会话已关闭")
            except Exception as exc:
                print(f"[警告] DSM 登出失败：{exc}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    if args.all:
        if args.task_key:
            raise RuntimeError("--all 和 --task-key 不能同时使用")
        return run_all(args)
    task_dir = select_task(args.source_dir.expanduser(), args.task_key)
    task_key = task_dir.name
    source = find_video(task_dir)
    remote_dir = f"/database/sop/{task_key}"
    remote_path = f"{remote_dir}/sop_video.mp4"

    print(f"[任务] task_key：{task_key}")
    print(f"[文件] 找到视频：{source}（{source.stat().st_size} 字节）")
    if source.suffix.lower() == ".mp4":
        print("[转码] 源文件已是 mp4，无需转码；上传时将重命名为 sop_video.mp4")
    else:
        print(f"[转码] 将使用 ffmpeg 转为临时文件 sop_video.mp4（H.264 + AAC，faststart）")
    print(f"[上传] 目标路径：{remote_path}")
    print(f"[JSON] 目标路径：{remote_dir}/sop_task.json")
    print(f"[上传] 覆盖已有文件：{'是' if args.replace_existing else '否'}")

    if args.dry_run:
        print("[DRY-RUN] 将生成 sop_task.json；不运行 ffmpeg，不登录 DSM，不创建目录，不上传文件")
        return 0

    upload_file = source
    temp_context: tempfile.TemporaryDirectory[str] | None = None
    client: DSMClient | None = None
    try:
        if source.suffix.lower() != ".mp4":
            temp_context = tempfile.TemporaryDirectory(prefix="upload_sop_")
            upload_file = Path(temp_context.name) / "sop_video.mp4"
            transcode(source, upload_file)
            print(f"[转码] 完成：{upload_file}（{upload_file.stat().st_size} 字节）")

        account = input("DSM 账号：").strip()
        password = getpass.getpass("DSM 密码（输入不会显示）：")
        if not account or not password:
            raise RuntimeError("DSM 账号和密码不能为空")

        print(f"[登录] 正在连接：{args.dsm_url}")
        client = DSMClient(args.dsm_url)
        client.login(account, password)
        password = ""
        print("[登录] 成功")

        print(f"[检查] 检查目标目录和同名文件：{remote_dir}")
        entries = client.ensure_folder_and_list(remote_dir)
        exists = any(item.get("name") == "sop_video.mp4" and not item.get("isdir") for item in entries)
        if exists and not args.replace_existing:
            print(f"[跳过] 远端文件已存在且未指定 --replace-existing：{remote_path}")
            upload_task_json(client, task_key, remote_dir)
            return 0
        if exists:
            print("[检查] 远端同名文件已存在，将按要求覆盖")
        else:
            print("[检查] 远端不存在同名文件，可以上传")

        print(f"[上传] 正在上传 {upload_file.stat().st_size} 字节...")
        client.upload(upload_file, remote_dir, args.replace_existing)
        upload_task_json(client, task_key, remote_dir)
        print(f"[成功] task_key={task_key}，远端路径={remote_path}，文件大小={upload_file.stat().st_size} 字节")
        return 0
    finally:
        if client and client.sid:
            try:
                client.logout()
                print("[登出] DSM 会话已关闭")
            except Exception as exc:
                print(f"[警告] DSM 登出失败：{exc}", file=sys.stderr)
        if temp_context:
            temp_context.cleanup()


def main() -> int:
    try:
        return run(parse_args())
    except DSMAPIError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"[错误] DSM 网络请求失败：{exc}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[取消] 用户中断操作", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
