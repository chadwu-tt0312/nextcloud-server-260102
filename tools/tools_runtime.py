#!/usr/bin/env python3
"""
Nextcloud 工具執行環境抽象層（Docker / Kubernetes）

供 run_batch_provision.py 等腳本共用：
- 複製檔案進 Pod/容器
- 以指定使用者執行指令
- 修正複製後檔案權限（www-data 可讀）

版本：1.0.0
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


CONTAINER_RUN_USER = "www-data"


@dataclass
class RuntimeTarget:
    """執行目標的顯示名稱（寫 log 用）。"""

    label: str


class NextcloudRuntime(ABC):
    """Docker 或 Kubernetes 上執行遠端指令的抽象介面。"""

    run_user: str = CONTAINER_RUN_USER

    @abstractmethod
    def resolve_target(self, logger: logging.Logger) -> RuntimeTarget | None:
        """解析或自動偵測執行目標。"""

    @abstractmethod
    def copy_file(self, local_path: str, remote_path: str) -> None:
        """複製本機檔案到遠端。"""

    @abstractmethod
    def fix_file_permissions(self, remote_paths: list[str], logger: logging.Logger) -> None:
        """複製後讓 run_user 可讀取遠端檔案。"""

    @abstractmethod
    def exec_as_user(
        self,
        user: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """以指定使用者執行遠端指令。"""

    def exec_run_user(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.exec_as_user(self.run_user, command, env=env)


class DockerRuntime(NextcloudRuntime):
    def __init__(self, container: str | None = None) -> None:
        self.container = container

    def resolve_target(self, logger: logging.Logger) -> RuntimeTarget | None:
        if self.container:
            return RuntimeTarget(label=self.container)
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error(f"❌ 無法執行 docker ps: {e}")
            return None
        for name in result.stdout.splitlines():
            if "nextcloud" in name.lower():
                self.container = name.strip()
                return RuntimeTarget(label=self.container)
        return None

    def copy_file(self, local_path: str, remote_path: str) -> None:
        assert self.container
        subprocess.run(
            ["docker", "cp", local_path, f"{self.container}:{remote_path}"],
            check=True,
        )

    def fix_file_permissions(self, remote_paths: list[str], logger: logging.Logger) -> None:
        assert self.container
        quoted = " ".join(remote_paths)
        cmd = f"chown {self.run_user}:{self.run_user} {quoted} && chmod 644 {quoted}"
        subprocess.run(
            ["docker", "exec", "-u", "root", self.container, "sh", "-c", cmd],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info(f"已設定遠端檔案權限（owner={self.run_user}）: {', '.join(remote_paths)}")

    def exec_as_user(
        self,
        user: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert self.container
        cmd = ["docker", "exec", "-u", user]
        if env:
            for key, value in env.items():
                cmd += ["-e", f"{key}={value}"]
        cmd += [self.container, *command]
        return subprocess.run(cmd, capture_output=True, text=True)


class K8sRuntime(NextcloudRuntime):
    OCC_PATH = "/var/www/html/occ"

    def __init__(
        self,
        namespace: str = "default",
        pod: str | None = None,
        label_selector: str = "app=nextcloud",
    ) -> None:
        self.namespace = namespace
        self.pod = pod
        self.label_selector = label_selector

    def resolve_target(self, logger: logging.Logger) -> RuntimeTarget | None:
        if self.pod:
            if self._ensure_running(logger):
                return RuntimeTarget(label=f"{self.namespace}/{self.pod}")
            return None
        try:
            result = subprocess.run(
                [
                    "kubectl", "get", "pods", "-n", self.namespace,
                    "-l", self.label_selector,
                    "-o", "jsonpath={.items[0].metadata.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            pod = result.stdout.strip()
            if not pod:
                result = subprocess.run(
                    [
                        "kubectl", "get", "pods", "-n", self.namespace,
                        "-o", "jsonpath={.items[?(@.metadata.name=~\"nextcloud.*\")].metadata.name}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                pod = result.stdout.split()[0].strip() if result.stdout.strip() else ""
            if not pod:
                logger.error(
                    f"❌ 在 namespace {self.namespace} 找不到 Nextcloud Pod，"
                    "請用 --pod 指定"
                )
                return None
            self.pod = pod
        except FileNotFoundError:
            logger.error("❌ 找不到 kubectl 命令")
            return None

        if self.pod and self._ensure_running(logger):
            return RuntimeTarget(label=f"{self.namespace}/{self.pod}")
        return None

    def _ensure_running(self, logger: logging.Logger) -> bool:
        assert self.pod
        result = subprocess.run(
            [
                "kubectl", "get", "pod", self.pod, "-n", self.namespace,
                "-o", "jsonpath={.status.phase}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        phase = result.stdout.strip()
        if phase != "Running":
            logger.error(f"❌ Pod {self.pod} 未運行（狀態: {phase or '未知'}）")
            return False
        return True

    def copy_file(self, local_path: str, remote_path: str) -> None:
        assert self.pod
        subprocess.run(
            ["kubectl", "cp", local_path, f"{self.namespace}/{self.pod}:{remote_path}"],
            check=True,
        )

    def _normalize_command(self, command: list[str]) -> list[str]:
        if len(command) >= 2 and command[0] == "php" and command[1] == "occ":
            return ["php", self.OCC_PATH, *command[2:]]
        return command

    def _kubectl_exec_as_user(
        self,
        user: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """kubectl exec 不支援 -u；以 su 切換使用者（與官方 Helm chart 用法一致）。"""
        command = self._normalize_command(command)
        parts: list[str] = []
        if env:
            parts.append("env")
            for key, value in env.items():
                parts.append(f"{key}={value}")
        parts.extend(command)
        inner = " ".join(shlex.quote(p) for p in parts)
        return [
            "kubectl", "exec", "-n", self.namespace, self.pod, "--",
            "su", "-s", "/bin/sh", user, "-c", inner,
        ]

    def fix_file_permissions(self, remote_paths: list[str], logger: logging.Logger) -> None:
        assert self.pod
        quoted = " ".join(shlex.quote(p) for p in remote_paths)
        cmd = f"chown {self.run_user}:{self.run_user} {quoted} && chmod 644 {quoted}"
        subprocess.run(
            self._kubectl_exec_as_user("root", ["sh", "-c", cmd]),
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info(f"已設定遠端檔案權限（owner={self.run_user}）: {', '.join(remote_paths)}")

    def exec_as_user(
        self,
        user: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert self.pod
        return subprocess.run(
            self._kubectl_exec_as_user(user, command, env=env),
            capture_output=True,
            text=True,
        )


def fetch_all_users_from_occ(
    runtime: NextcloudRuntime,
    logger: logging.Logger,
) -> tuple[bool, str, list[str]]:
    """
    透過遠端 occ user:list 取得所有使用者 ID（不需 OCS 環境變數）

    Returns:
        (success, message, user_ids)
    """
    proc = runtime.exec_run_user(["php", "occ", "user:list", "--output=json"])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "occ user:list 失敗").strip()
        logger.error(err)
        return False, err, []
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            return False, f"occ 回傳格式不正確: 預期 dict，收到 {type(data)}", []
        users = list(data.keys())
        logger.info(f"透過 occ 取得 {len(users)} 位使用者")
        return True, "成功取得使用者列表", users
    except json.JSONDecodeError as e:
        return False, f"occ JSON 解析失敗: {e}", []


def create_runtime(
    runtime: str,
    *,
    container: str | None = None,
    namespace: str = "default",
    pod: str | None = None,
    label_selector: str = "app=nextcloud",
) -> NextcloudRuntime:
    if runtime == "docker":
        return DockerRuntime(container=container)
    if runtime == "k8s":
        return K8sRuntime(namespace=namespace, pod=pod, label_selector=label_selector)
    raise ValueError(f"不支援的 runtime: {runtime}")
