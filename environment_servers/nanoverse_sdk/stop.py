# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One pinned Harness leader for the frozen sole-actor native launch protocol."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path


def _command_matches(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    return actual == expected or bool(expected) and actual == (expected[0], "-B", *expected[1:])


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    start: str
    argv: tuple[str, ...]


def owned_processes(root: int) -> dict[int, ProcessIdentity]:
    parents = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            parents[int(entry.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {root}
    while True:
        children = {pid for pid, (parent, _) in parents.items() if parent in owned}
        if children <= owned:
            break
        owned |= children
    result = {}
    for pid in owned & parents.keys():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0")
            argv = tuple(os.fsdecode(arg) for arg in raw.split(b"\0"))
            parent, start = parents[pid]
            result[pid] = ProcessIdentity(pid, parent, start, argv)
        except OSError:
            continue
    return result


@dataclass
class HarnessStop:
    descriptor: int
    chain: tuple[ProcessIdentity, ...]

    @classmethod
    def bind(cls, supervisor: int, driver: tuple[str, ...], cli: tuple[str, ...], harness: tuple[str, ...]):
        rows = owned_processes(supervisor)
        candidates = []
        for row in rows.values():
            if not _command_matches(row.argv, harness):
                continue
            chain = [row]
            while chain[-1].pid != supervisor:
                parent = rows.get(chain[-1].parent)
                if parent is None or parent in chain:
                    break
                chain.append(parent)
            if (
                len(chain) >= 4
                and chain[-1].pid == supervisor
                and _command_matches(chain[-2].argv, driver)
                and _command_matches(chain[-3].argv, cli)
            ):
                candidates.append(tuple(chain))
        if len(candidates) != 1:
            return None
        chain = candidates[0]
        try:
            descriptor = os.pidfd_open(chain[0].pid)
        except (OSError, AttributeError):
            return None
        target = cls(descriptor, chain)
        if not target.valid():
            target.close()
            return None
        return target

    def valid(self) -> bool:
        rows = owned_processes(self.chain[-1].pid)
        return all(rows.get(row.pid) == row for row in self.chain)

    def request(self) -> bool:
        if not self.valid():
            return False
        try:
            signal.pidfd_send_signal(self.descriptor, signal.SIGTERM, None, 0)
        except (OSError, AttributeError):
            return False
        return True

    def close(self) -> None:
        os.close(self.descriptor)
