#!/usr/bin/env python3
"""从 NAS verified episode 视频提取最后一帧并上传到 SOP 目录。"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import re
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
MAX_SEARCH_DEPTH = 12
EPISODE_RE = re.compile(r"^episode_(\d+)(?:_|$)")


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
            "api": "SYNO.API.Auth", "version": 6, "method": "login",
            "account": account, "passwd": password, "session": "FileStation", "format": "sid",
        }
        payload = self._decode_response(
            "登录", self.http.get(self.entry_url, params=params, timeout=self.timeout)
        )
        sid = payload.get("data", {}).get("sid")
        if not sid:
            raise RuntimeError("登录响应成功，但没有返回 sid")
        self.sid = sid
        self.http.cookies.set("id", sid)

    def logout(self) -> None:
        if not self.sid:
            return
        params = {
            "api": "SYNO.API.Auth", "version": 6, "method": "logout",
            "session": "FileStation", "_sid": self.sid,
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
        return {"api": api, "version": 2, "method": method, "_sid": self.sid, **kwargs}

    def get_info(self, remote_path: str) -> list[dict[str, Any]]:
        params = self._params(
            "SYNO.FileStation.List", "getinfo", path=json.dumps([remote_path]),
            additional=json.dumps(["size"]),
        )
        response = self.http.post(self.entry_url, data=params, timeout=self.timeout)
        payload = self._decode_response(f"查询远端路径 {remote_path}", response)
        return payload.get("data", {}).get("files", [])

    def exists(self, remote_path: str) -> bool:
        try:
            info = self.get_info(remote_path)
            # 部分 DSM 版本会让 getinfo 请求整体 success=true，但在单个条目中
            # 返回 error；这种条目不能视为路径存在。
            return any(not item.get("error") for item in info)
        except DSMAPIError as exc:
            if exc.code == 408:
                return False
            raise

    def list_folder(self, remote_dir: str) -> list[dict[str, Any]]:
        operation = f"列出远端目录 {remote_dir}"
        page_size = 200
        offset = 0
        entries: list[dict[str, Any]] = []
        while True:
            params = self._params(
                "SYNO.FileStation.List", "list", folder_path=remote_dir,
                offset=offset, limit=page_size,
            )
            for attempt in range(1, 6):
                response = self.http.post(self.entry_url, data=params, timeout=self.timeout)
                if response.status_code not in {502, 503, 504} or attempt == 5:
                    payload = self._decode_response(operation, response)
                    break
                delay = 2 ** (attempt - 1)
                print(
                    f"[重试] {operation} 第 offset={offset} 页收到 HTTP "
                    f"{response.status_code}，第 {attempt}/5 次重试，{delay} 秒后继续"
                )
                time.sleep(delay)

            data = payload.get("data", {})
            page = data.get("files", [])
            entries.extend(page)
            total = data.get("total")
            offset += len(page)
            if remote_dir == "/database/verified" and page:
                total_text = str(total) if isinstance(total, int) else "未知"
                print(f"[分页] verified 已读取 {offset}/{total_text} 个目录项")
            if not page or len(page) < page_size:
                break
            if isinstance(total, int) and offset >= total:
                break
        return entries

    def create_folder(self, remote_dir: str) -> None:
        path = PurePosixPath(remote_dir)
        params = self._params(
            "SYNO.FileStation.CreateFolder", "create", folder_path=str(path.parent),
            name=path.name, force_parent="true",
        )
        self._decode_response(
            f"创建远端目录 {remote_dir}",
            self.http.post(self.entry_url, data=params, timeout=self.timeout),
        )

    def ensure_folder(self, remote_dir: str) -> None:
        if not self.exists(remote_dir):
            print(f"[目录] 创建目标目录：{remote_dir}")
            self.create_folder(remote_dir)

    def upload(
        self, local_file: Path, remote_dir: str, overwrite: bool,
        remote_name: str, content_type: str,
    ) -> None:
        params = self._params(
            "SYNO.FileStation.Upload", "upload", path=remote_dir,
            create_parents="true", overwrite=str(overwrite).lower(),
        )
        with local_file.open("rb") as stream:
            response = self.http.post(
                self.entry_url, data=params,
                files={"file": (remote_name, stream, content_type)}, timeout=self.timeout,
            )
        self._decode_response(f"上传 {remote_name}", response)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        params = self._params(
            "SYNO.FileStation.Download", "download", path=remote_path, mode="download"
        )
        params.pop("_sid", None)
        with self.http.get(
            self.entry_url, params=params, timeout=self.timeout, stream=True
        ) as response:
            if not response.ok:
                raise RuntimeError(
                    f"下载 {remote_path} HTTP 请求失败，状态码：{response.status_code}"
                )
            if response.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._decode_response(f"下载 {remote_path}", response)
            with local_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)

    def download_text(self, remote_path: str) -> str:
        if not self.exists(remote_path):
            return ""
        with tempfile.TemporaryDirectory(prefix="frame_log_download_") as temp_dir:
            local_path = Path(temp_dir) / "upload_log.txt"
            self.download_file(remote_path, local_path)
            try:
                return local_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"远端日志不是 UTF-8 文本：{remote_path}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量提取 verified 视频最后一帧并上传 NAS")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="task_key 来源目录")
    parser.add_argument("--dsm-url", default=DEFAULT_DSM_URL, help="DSM 地址")
    parser.add_argument("--dry-run", action="store_true", help="只发现并打印，不下载、截帧或上传")
    return parser.parse_args()


def read_run_scope() -> tuple[str, str]:
    date = input("日期（YYYYMMDD）：").strip()
    operator = input("操作员拼音：").strip()
    if not re.fullmatch(r"\d{8}", date):
        raise RuntimeError("日期格式必须是 YYYYMMDD")
    try:
        dt.datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError("日期不是有效的日历日期") from exc
    if not operator or operator in {".", ".."} or "/" in operator:
        raise RuntimeError("操作员拼音不能为空或包含路径分隔符")
    return date, operator


def task_keys(source_dir: Path) -> list[str]:
    if not source_dir.is_dir():
        raise RuntimeError(f"task_key 来源目录不存在或不是目录：{source_dir}")
    keys = sorted(path.name for path in source_dir.iterdir() if path.is_dir())
    if not keys:
        raise RuntimeError(f"来源目录下没有 task 子文件夹：{source_dir}")
    return keys


def current_minute() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def upload_task_json(client: DSMClient, task_key: str, remote_dir: str) -> None:
    payload = task_json_payload(task_key)
    with tempfile.TemporaryDirectory(prefix="frame_sop_task_json_") as temp_dir:
        local_json = Path(temp_dir) / "sop_task.json"
        local_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        client.upload(
            local_json, remote_dir, True, "sop_task.json",
            "application/json; charset=utf-8",
        )
    print(f"[JSON] 已覆盖上传：{remote_dir}/sop_task.json")


def task_json_payload(task_key: str) -> dict[str, Any]:
    return {
        "task_id": "",
        "task_key": task_key,
        "nas_path": f"/volume1/database/sop/{task_key}/sop_video.mp4",
        "replace_existing": False,
    }


def upload_all_task_json(client: DSMClient, successful_keys: list[str]) -> None:
    payload = [task_json_payload(task_key) for task_key in successful_keys]
    with tempfile.TemporaryDirectory(prefix="frame_sop_tasks_json_") as temp_dir:
        local_json = Path(temp_dir) / "sop_tasks.json"
        local_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        client.upload(
            local_json, "/database/sop", True, "sop_tasks.json",
            "application/json; charset=utf-8",
        )
    print(
        f"[汇总 JSON] 已覆盖上传 /database/sop/sop_tasks.json，"
        f"包含 {len(successful_keys)} 个 task"
    )


def select_episode(client: DSMClient, operator_dir: str) -> tuple[int, str, str] | None:
    candidates: list[tuple[int, str, str]] = []
    for entry in client.list_folder(operator_dir):
        name = str(entry.get("name", ""))
        match = EPISODE_RE.match(name)
        if entry.get("isdir") and match:
            path = str(entry.get("path") or f"{operator_dir}/{name}")
            candidates.append((int(match.group(1)), name, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))


def discover_videos(client: DSMClient, episode_path: str) -> list[tuple[str, str]]:
    videos: list[tuple[str, str]] = []
    for entry in client.list_folder(episode_path):
        folder = str(entry.get("name", ""))
        prefix = "observation.image."
        if not entry.get("isdir") or not folder.startswith(prefix):
            continue
        modality = folder[len(prefix):]
        if modality.startswith("flow_"):
            print(f"[跳过] flow modality：{folder}")
            continue
        folder_path = str(entry.get("path") or f"{episode_path}/{folder}")
        video_path = f"{folder_path}/video.mp4"
        if client.exists(video_path):
            videos.append((modality, video_path))
    return sorted(videos)


def extract_last_frame(input_path: Path, output_path: Path) -> tuple[bool, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先安装并确保 ffmpeg 在 PATH 中")
    commands = [
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-sseof", "-3", "-i",
         str(input_path), "-vf", "reverse", "-frames:v", "1", "-q:v", "2",
         str(output_path), "-y"],
        # 若末尾 3 秒因关键帧或文件尾损坏无法解码，扩大到末尾 10 秒重试。
        # 只反转短片段，避免对整段长视频使用 reverse 占用大量内存。
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-sseof", "-10", "-i",
         str(input_path), "-vf", "reverse", "-frames:v", "1", "-q:v", "2",
         str(output_path), "-y"],
    ]
    errors: list[str] = []
    for attempt, command in enumerate(commands, 1):
        output_path.unlink(missing_ok=True)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size:
            return True, f"第 {attempt} 种方法"
        detail = result.stderr.strip().splitlines()
        errors.append(detail[-1] if detail else f"退出码 {result.returncode}")
    return False, "；".join(errors)


def find_verified_task_dirs(client: DSMClient, keys: list[str]) -> dict[str, str]:
    """通过 List 返回的真实目录名和路径，精确匹配本地 SOP task_key。"""
    wanted = set(keys)
    found: dict[str, str] = {}
    for entry in client.list_folder("/database/verified"):
        name = str(entry.get("name", ""))
        if entry.get("isdir") and name in wanted:
            found[name] = str(entry.get("path") or f"/database/verified/{name}")
    return found


def locate_operator_dirs(
    client: DSMClient, task_dir: str, date: str, operator: str
) -> tuple[list[str], str | None]:
    """在已由 List 精确匹配的 task 根目录下递归查找 date/operator。"""

    # (目录路径, 相对 task 根目录的深度)。只沿目录向下搜索，不对名称做模糊匹配。
    pending: list[tuple[str, int]] = [(task_dir, 0)]
    matches: list[str] = []
    found_date = False
    while pending:
        current, depth = pending.pop(0)
        try:
            current_entries = client.list_folder(current)
        except DSMAPIError as exc:
            # 目录可能在遍历期间被移动/删除，或是 DSM 返回的不可列出条目。
            if exc.code == 408:
                print(f"[跳过目录] 路径已不存在或不可列出：{current}")
                continue
            raise
        for entry in current_entries:
            if not entry.get("isdir"):
                continue
            name = str(entry.get("name", ""))
            path = str(entry.get("path") or f"{current}/{name}")
            child_depth = depth + 1
            if name == date:
                found_date = True
                try:
                    date_entries = client.list_folder(path)
                except DSMAPIError as exc:
                    if exc.code == 408:
                        continue
                    raise
                for date_entry in date_entries:
                    if date_entry.get("isdir") and str(date_entry.get("name", "")) == operator:
                        matches.append(
                            str(date_entry.get("path") or f"{path}/{operator}")
                        )
                # 日期目录是搜索终点；不继续遍历日期内部的其他操作员和 episode。
                continue
            # 日期是底层结构的一部分。其他日期目录不可能包含本次目标，直接剪枝。
            if re.fullmatch(r"\d{8}", name):
                continue
            if child_depth < MAX_SEARCH_DEPTH:
                pending.append((path, child_depth))

    if matches:
        return sorted(set(matches)), None
    if found_date:
        return [], "操作员不存在"
    return [], f"在 task 下 {MAX_SEARCH_DEPTH} 层内未找到日期"


def run(args: argparse.Namespace) -> int:
    date, operator = read_run_scope()
    keys = task_keys(args.source_dir.expanduser())
    print(
        f"[SOP 范围] 只处理本地 {args.source_dir.expanduser()} 下的 {len(keys)} 个精确 task_key；"
        "不会扫描该操作员当天的其他 task"
    )
    account = input("DSM 账号：").strip()
    password = getpass.getpass("DSM 密码（输入不会显示）：")
    if not account or not password:
        raise RuntimeError("DSM 账号和密码不能为空")

    client = DSMClient(args.dsm_url)
    counts: dict[str, int] = {}
    dry_run_counts: dict[str, int] = {}
    unmatched: list[str] = []
    failed_videos: list[str] = []
    log_records: list[str] = []
    try:
        print(f"[登录] 正在连接：{args.dsm_url}")
        client.login(account, password)
        password = ""
        print(f"[登录] 成功，共 {len(keys)} 个 task_key")
        print("[查找] 正在列出 /database/verified，并精确匹配本地 SOP task_key")
        verified_task_dirs = find_verified_task_dirs(client, keys)
        print(
            f"[查找] verified 中匹配到 {len(verified_task_dirs)}/{len(keys)} 个 SOP task_key"
        )

        for index, task_key in enumerate(keys, 1):
            task_root = verified_task_dirs.get(task_key)
            if not task_root:
                print(f"\n[任务 {index}/{len(keys)}] {task_key}")
                print(f"[留空] verified 中没有精确匹配的 task_key 目录")
                unmatched.append(f"{task_key}（verified 中没有 task_key）")
                continue
            print(
                f"\n[任务 {index}/{len(keys)}] {task_key}，在 {task_root} 下递归查找 "
                f"*/{date}/{operator}（最多 {MAX_SEARCH_DEPTH} 层）"
            )
            try:
                operator_dirs, reason = locate_operator_dirs(client, task_root, date, operator)
                if reason:
                    print(f"[留空] {task_key}：{reason}")
                    unmatched.append(f"{task_key}（{reason}）")
                    continue
                print(f"[路径匹配] 找到 {len(operator_dirs)} 个 date/operator 候选：")
                for operator_dir in operator_dirs:
                    print(f"  - {operator_dir}")

                episode_candidates: list[tuple[int, str, str, str]] = []
                for operator_dir in operator_dirs:
                    selected = select_episode(client, operator_dir)
                    if selected:
                        number, episode_name, episode_path = selected
                        episode_candidates.append(
                            (number, episode_name, episode_path, operator_dir)
                        )
                    else:
                        print(f"[跳过候选] 没有 episode_*：{operator_dir}")
                if not episode_candidates:
                    print(f"[未找到匹配] {task_key}：所有操作员候选目录都没有 episode_* 文件夹")
                    unmatched.append(f"{task_key}（没有 episode_*）")
                    continue
                _, episode_name, episode_path, chosen_operator_dir = max(
                    episode_candidates, key=lambda item: (item[0], item[2])
                )
                if len(episode_candidates) > 1:
                    print(
                        f"[多路径选择] 按最大 episode 编号、再按完整路径排序选择："
                        f"{chosen_operator_dir}"
                    )
                print(f"[episode] 已选择编号最大的：{episode_name}（{episode_path}）")
                videos = discover_videos(client, episode_path)
                if not videos:
                    print(f"[未找到匹配] {task_key}：选中 episode 中没有可处理的 video.mp4")
                    unmatched.append(f"{task_key}（没有可处理视频）")
                    continue

                for modality, remote_video in videos:
                    output_name = f"{task_key}_{modality}_lastframe.png"
                    print(f"[发现] {remote_video} -> {output_name}")
                if args.dry_run:
                    dry_run_counts[task_key] = len(videos)
                    print(f"[DRY-RUN] 将处理 {len(videos)} 个视频并上传到 /database/sop/{task_key}/")
                    print(f"[DRY-RUN] 将生成并覆盖 /database/sop/{task_key}/sop_task.json")
                    continue

                remote_output_dir = f"/database/sop/{task_key}"
                client.ensure_folder(remote_output_dir)
                success_count = 0
                with tempfile.TemporaryDirectory(prefix="extract_frames_") as temp_dir:
                    temp = Path(temp_dir)
                    for number, (modality, remote_video) in enumerate(videos):
                        local_video = temp / f"input_{number}.mp4"
                        output_name = f"{task_key}_{modality}_lastframe.png"
                        local_png = temp / output_name
                        try:
                            print(f"[下载] {remote_video}")
                            client.download_file(remote_video, local_video)
                            ok, detail = extract_last_frame(local_video, local_png)
                            if not ok:
                                raise RuntimeError(f"两种 ffmpeg 方法均失败：{detail}")
                            print(f"[截帧成功] {remote_video}（{detail}）")
                            client.upload(local_png, remote_output_dir, True, output_name, "image/png")
                            print(f"[上传成功] {remote_output_dir}/{output_name}")
                            success_count += 1
                        except (DSMAPIError, requests.RequestException, RuntimeError, OSError) as exc:
                            failed_videos.append(f"{remote_video}：{exc}")
                            print(f"[截取失败] {remote_video}：{exc}", file=sys.stderr)
                        finally:
                            local_video.unlink(missing_ok=True)
                            local_png.unlink(missing_ok=True)
                if success_count:
                    upload_task_json(client, task_key, remote_output_dir)
                    counts[task_key] = success_count
                    log_records.append(
                        f"{current_minute()}\t{task_key}\tframe_extract\t"
                        f"date={date} operator={operator} episode={episode_name} frames={success_count}"
                    )
                else:
                    print(f"[任务失败] {task_key} 没有成功提取并上传任何图片", file=sys.stderr)
            except (DSMAPIError, requests.RequestException, RuntimeError, OSError) as exc:
                failed_videos.append(f"{task_key}（任务级错误）：{exc}")
                print(f"[任务失败] {task_key}：{exc}", file=sys.stderr)

        if args.dry_run:
            print(
                f"\n[DRY-RUN] 将用 {len(dry_run_counts)} 个 task 覆盖生成 "
                "/database/sop/sop_tasks.json"
            )
            print("[DRY-RUN] 未下载、未截帧、未创建目录、未上传、未更新日志")
        else:
            if counts:
                try:
                    upload_all_task_json(client, list(counts))
                except (DSMAPIError, requests.RequestException, RuntimeError, OSError) as exc:
                    failed_videos.append(f"sop_tasks.json（汇总上传失败）：{exc}")
                    print(f"[汇总 JSON 失败] {exc}", file=sys.stderr)

        if not args.dry_run and log_records:
            log_path = "/database/sop/upload_log.txt"
            print(f"\n[日志] 下载已有内容并追加 {len(log_records)} 条记录：{log_path}")
            old_log = client.download_text(log_path)
            combined = old_log
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += "\n".join(log_records) + "\n"
            with tempfile.TemporaryDirectory(prefix="frame_upload_log_") as temp_dir:
                local_log = Path(temp_dir) / "upload_log.txt"
                local_log.write_text(combined, encoding="utf-8")
                client.upload(local_log, "/database/sop", True, "upload_log.txt", "text/plain; charset=utf-8")
            print("[日志] 覆盖上传成功")
        elif not args.dry_run:
            print("\n[日志] 本次没有成功处理的 task_key，不更新日志")

        if args.dry_run:
            print(f"\n[DRY-RUN 汇总] 计划处理 {len(dry_run_counts)} 个 task_key")
            for task_key, count in dry_run_counts.items():
                print(f"[计划明细] {task_key}：预计 {count} 张")
        else:
            print(f"\n[汇总] 成功处理 {len(counts)} 个 task_key")
            for task_key, count in counts.items():
                print(f"[成功明细] {task_key}：{count} 张")
        print(f"[未找到匹配] {', '.join(unmatched) if unmatched else '无'}")
        if failed_videos:
            print("[提取失败的视频]")
            for item in failed_videos:
                print(f"  - {item}")
        else:
            print("[提取失败的视频] 无")
        return 1 if failed_videos else 0
    finally:
        if client.sid:
            try:
                client.logout()
                print("[登出] DSM 会话已关闭")
            except Exception as exc:
                print(f"[警告] DSM 登出失败：{exc}", file=sys.stderr)


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
