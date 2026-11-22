#!/usr/bin/env python3
"""Pull images, select running Compose projects to update, and offer Docker prune."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Dict, Tuple


LABEL = "com.docker.compose."
INSPECT_FORMAT = '[{{json .Image}},{{json .Config.Labels}}]'
Image = Tuple[str, str]  # Image reference and optional platform.


def docker(*args, cwd=None, capture=False, timeout=None):
    if timeout is not None:
        # Docker starts Compose as a plugin subprocess. Stop both on timeout.
        with subprocess.Popen(
            ["docker", *args], cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True,
        ) as process:
            try:
                status = process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
            if status:
                raise subprocess.CalledProcessError(status, process.args)
        return None

    result = subprocess.run(
        ["docker", *args], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        encoding="utf-8", check=True,
    )
    return result.stdout


def running_containers(*filters):
    args = ["ps", "--quiet", "--no-trunc", "--filter", f"label={LABEL}project"]
    for label in filters:
        args.extend(("--filter", f"label={LABEL}{label}"))
    ids = docker(*args, capture=True).split()
    if not ids:
        return []
    lines = docker(
        "inspect", "--type", "container", "--format", INSPECT_FORMAT, *ids,
        capture=True,
    )
    containers = map(json.loads, lines.splitlines())
    return [(image_id, labels) for image_id, labels in containers
            if labels.get(LABEL + "oneoff", "").lower() != "true"]


@dataclass(frozen=True)
class Deployment:
    directory: Path
    files: Tuple[str, ...]
    env_files: Tuple[str, ...]

    @classmethod
    def from_labels(cls, labels):
        files = tuple(labels.get(LABEL + "project.config_files", "").split(","))
        directory = labels.get(LABEL + "project.working_dir") or Path(files[0]).parent
        env_files = labels.get(LABEL + "project.environment_file", "")
        return cls(Path(directory), files, tuple(env_files.split(",")) if env_files else ())

    def extends(self, other):
        return (self.directory.resolve() == other.directory.resolve()
                and self.env_files == other.env_files
                and self.files[:len(other.files)] == other.files)


@dataclass
class Stack:
    name: str
    deployment: Deployment
    running: dict

    def compose(self, *args, capture=False, timeout=None):
        params = [
            "compose", "--ansi", "never", "--project-name", self.name,
            "--project-directory", str(self.deployment.directory), "--profile", "*",
        ]
        for file in self.deployment.files:
            params.extend(("-f", file))
        for file in self.deployment.env_files:
            params.extend(("--env-file", file))
        return docker(
            *params, *args, cwd=self.deployment.directory, capture=capture, timeout=timeout,
        )

    def config(self):
        return json.loads(self.compose("config", "--format", "json", capture=True))


@dataclass
class Scan:
    stack: Stack
    config: dict
    images: Dict[str, Image]


def scan_stack(root, name, containers):
    deployments = {Deployment.from_labels(labels) for _, labels in containers}
    # An older container can still have the base file before an override was added.
    in_repo = [
        item for item in deployments
        if root == item.directory.resolve() or root in item.directory.resolve().parents
    ]
    if not in_repo:
        return None
    deployment = max(in_repo, key=lambda item: len(item.files))
    if not all(deployment.extends(item) for item in deployments):
        raise ValueError("运行容器记录了不同的 Compose 配置，无法确定部署参数")
    if not all(deployment.files):
        raise ValueError("运行容器缺少 Compose 文件路径")
    running = defaultdict(set)
    for image_id, labels in containers:
        running[labels[LABEL + "service"]].add(image_id)
    stack = Stack(name, deployment, dict(sorted(running.items())))
    config = stack.config()
    images = {}
    for service in stack.running:
        definition = config.get("services", {}).get(service)
        if definition is None:
            raise ValueError(f"{service} 仍在运行，但当前 Compose 配置中没有该服务")
        if definition.get("image"):
            images[service] = (definition["image"], definition.get("platform", ""))
    return Scan(stack, config, images)


def image_ids(scan):
    result = {}
    resolved = {}
    for service, image in scan.images.items():
        if image not in resolved:
            ref, platform = image
            platform_args = ("--platform", platform) if platform else ()
            resolved[image] = docker(
                "image", "inspect", "--format", "{{.Id}}", *platform_args, ref,
                capture=True,
            ).strip()
        result[service] = resolved[image]
    return result


def short_id(image_id):
    return image_id.split(":")[-1][:12]


def select_updates(count):
    while True:
        value = input("\n选更新项目（如 1,3，all 全选，回车跳过，q 退出）> ").strip().lower()
        if value == "q":
            return None
        if value == "all":
            return list(range(count))
        if not value:
            return []
        numbers = re.split(r"[\s,]+", value)
        if all(part.isascii() and part.isdecimal() for part in numbers):
            try:
                selected = list(dict.fromkeys(int(part) - 1 for part in numbers))
            except ValueError:
                selected = []
            if selected and all(0 <= index < count for index in selected):
                return selected
        print("请输入列表中的编号、all 或 q。")


def report_failure(name, stage, error):
    if isinstance(error, subprocess.CalledProcessError):
        # Docker has already printed stderr. Do not print captured Compose config.
        error = f"Docker 退出码 {error.returncode}"
    elif isinstance(error, subprocess.TimeoutExpired):
        error = f"拉取镜像超过 {error.timeout} 秒"
    print(f"[{name}] {stage}失败（{error}）", file=sys.stderr, flush=True)


def run(root, check_only, pull_timeout):
    projects = defaultdict(list)
    for image_id, labels in running_containers():
        projects[labels[LABEL + "project"]].append((image_id, labels))

    failed = False
    scans = []
    for name, containers in sorted(projects.items()):
        try:
            scan = scan_stack(root, name, containers)
            if scan is None:
                continue
            print(f"[{name}] 检查镜像", flush=True)
            if scan.images:
                scan.stack.compose(
                    "pull", "--policy", "always", "--quiet", *scan.images,
                    timeout=pull_timeout,
                )
            scans.append(scan)
        except (subprocess.SubprocessError, ValueError, OSError) as error:
            report_failure(name, "检查", error)
            failed = True

    updates = []
    # Resolve tags after all pulls, since multiple stacks can share a tag.
    for scan in scans:
        stack = scan.stack
        try:
            target_ids = image_ids(scan)
        except (subprocess.CalledProcessError, OSError) as error:
            report_failure(stack.name, "读取目标镜像", error)
            failed = True
            continue
        changed = [service for service, target in target_ids.items()
                   if stack.running[service] != {target}]
        if not changed:
            status = "已部署当前镜像" if scan.images else "当前运行的服务没有可拉取的镜像"
            print(f"[{stack.name}] {status}", flush=True)
            continue
        updates.append((scan, target_ids))
        print(f"{len(updates)}) {stack.name}")
        for service in changed:
            current = ", ".join(sorted(map(short_id, stack.running[service])))
            print(f"   {service} ({scan.images[service][0]}) "
                  f"{current} -> {short_id(target_ids[service])}")

    if not updates:
        print("未完成全部项目的检查。" if failed else "没有待更新的项目。")
    if check_only:
        return int(failed)

    selected = select_updates(len(updates)) if updates else []
    if selected is None:
        return int(failed)
    for index in selected:
        scan, target_ids = updates[index]
        stack = scan.stack
        try:
            active = running_containers(f"project={stack.name}")
            current_services = {labels[LABEL + "service"] for _, labels in active}
            if current_services != stack.running.keys():
                raise ValueError("运行中的服务已变化，请重新检查")
            if stack.config() != scan.config or image_ids(scan) != target_ids:
                raise ValueError("Compose 配置或目标镜像已变化，请重新检查")
            print(f"[{stack.name}] 正在更新", flush=True)
            stack.compose("up", "-d", "--pull", "never", "--no-build", *stack.running)
            deployed = defaultdict(set)
            for image_id, labels in running_containers(f"project={stack.name}"):
                deployed[labels[LABEL + "service"]].add(image_id)
            incomplete = [
                service for service in stack.running
                if service not in deployed
                or (service in target_ids and deployed[service] != {target_ids[service]})
            ]
            if incomplete:
                raise ValueError(f"未确认目标镜像正在运行：{', '.join(incomplete)}")
            print(f"[{stack.name}] 已更新", flush=True)
        except (subprocess.CalledProcessError, ValueError, OSError) as error:
            report_failure(stack.name, "更新", error)
            failed = True

    if scans:
        print("\nDocker 清理范围为当前 Docker 主机上的已停止容器、"
              "未使用的网络、悬空镜像和未使用的构建缓存。")
        if input("执行 docker system prune？[y/N] ").strip().lower() in {"y", "yes"}:
            docker("system", "prune", "--force")
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description="检查并按选择更新此目录下运行中的 Compose 项目")
    parser.add_argument("--check", action="store_true", help="只拉取镜像并列出待更新项目")
    parser.add_argument(
        "--pull-timeout", type=int, default=600, metavar="SECONDS",
        help="每个项目拉取镜像的超时秒数，默认 600",
    )
    args = parser.parse_args()
    if args.pull_timeout < 1:
        parser.error("--pull-timeout 必须为正整数")
    root = Path(__file__).resolve().parent
    lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        print("此目录已有镜像更新检查正在执行。", file=sys.stderr)
        return 1
    try:
        return run(root, args.check, args.pull_timeout)
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EOFError:
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
    except (subprocess.CalledProcessError, OSError) as error:
        report_failure("Docker", "执行", error)
        sys.exit(1)
