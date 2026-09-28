"""Installing a skill from a local folder: validate, copy, publish atomically.

Importing copies the whole package — SKILL.md, scripts, references — into a
directory easycode owns. The source folder is read once and never referred to
again: a skill that kept pointing at where it was downloaded would break the
moment that folder moved, and "installed" has to mean installed.

Nothing here executes anything from the source. A script inside a skill runs
later, if at all, through the ordinary Shell tool and its own permissions.

The published directory is created by a single no-replace rename, so two
imports of the same name cannot leave a half-written half of each: whichever
loses the rename reports a conflict and its staging directory is removed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from easycode.extensions.skills import MCP_COMMAND_PREFIX, Skill, SkillLoadError, load_skill

#: Fixed ceilings on one import. They are not configuration: a package past any
#: of them is refused with the reason, never partially copied.
MAX_ENTRIES = 10_000
MAX_BYTES = 100 * 1024 * 1024
MAX_DEPTH = 64


class SkillImportError(ValueError):
    """The folder cannot be installed as a skill."""


class SkillConflictError(SkillImportError):
    """Something is already installed under this skill's name."""


def import_skill(source: Path, destination_root: Path, scope: str) -> Skill:
    """Install the skill package at ``source`` under ``destination_root``.

    ``destination_root`` is decided by the caller (the route), never by the
    request body or the source's content: a skill's own frontmatter must not be
    able to choose where it lands.

    Returns the skill as it reads back from its installed location, so nothing
    downstream can keep using the source path as its resource directory.
    """
    src = _resolve_source(source)
    registry_source = "user" if scope == "personal" else "project"
    staged_name = _validated_name(src, registry_source)
    destination_root = Path(destination_root)
    _assert_install_root(destination_root)
    _assert_disjoint(src, destination_root)
    dest = destination_root / src.name
    _assert_no_conflict(dest, staged_name, destination_root)

    destination_root.mkdir(parents=True, exist_ok=True)
    # Staged beside the install root — same filesystem, and outside the
    # directory skills are discovered from, so a half-copied package is never
    # a loadable one.
    staging = destination_root.parent / f".skill-import-{uuid.uuid4().hex}"
    try:
        _copy_tree(src, staging)
        # Read back what was actually written: the copy has to be a skill on its
        # own, not just a directory that happened to come from one. The result
        # is unused on purpose — the same parse runs again on the published
        # copy, and this call exists to refuse the publish before it happens.
        load_skill(staging, registry_source)
        # Checked again here, immediately before publishing: another import of
        # the same name may have landed while this one was copying.
        _assert_no_conflict(dest, staged_name, destination_root)
        _rename_exclusive(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_skill(dest, registry_source)


def _resolve_source(source: Path) -> Path:
    try:
        src = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillImportError(f"无法读取源文件夹: {source}（{exc}）") from exc
    if not src.is_dir():
        raise SkillImportError(f"源路径不是文件夹: {src}")
    return src


def _validated_name(src: Path, registry_source: str) -> str:
    """The skill's command name, checked against what the importer may create."""
    try:
        skill = load_skill(src, registry_source)
    except SkillLoadError as exc:
        raise SkillImportError(str(exc)) from exc
    name = skill.name
    if not name:
        raise SkillImportError("Skill 名称为空")
    if any(ch.isspace() for ch in name) or "/" in name or "\\" in name:
        raise SkillImportError(f"Skill 名称不能含空白或路径分隔符: {name!r}")
    if name.startswith(MCP_COMMAND_PREFIX):
        raise SkillImportError(
            f"Skill 名称不能以 {MCP_COMMAND_PREFIX} 开头（该前缀保留给 MCP 服务命令）: {name!r}"
        )
    return name


def _assert_install_root(destination_root: Path) -> None:
    """The last two components must be real directories, not symlinks.

    ``<root>/.easycode/skills`` is computed by the server from an already
    resolved project (or from the data home). A link standing in for either
    component would send the install somewhere else entirely, so it is refused
    rather than followed.
    """
    if not destination_root.is_absolute():
        raise SkillImportError(f"安装目录必须是绝对路径: {destination_root}")
    for part in (destination_root.parent, destination_root):
        if part.is_symlink():
            raise SkillImportError(f"安装路径不能经过符号链接: {part}")


def _assert_disjoint(src: Path, destination_root: Path) -> None:
    """Refuse a source that contains, or lives inside, the install root."""
    for a, b, message in (
        (src, destination_root, "源文件夹在安装目录内，导入会复制自身"),
        (destination_root, src, "安装目录在源文件夹内，导入会复制自身"),
    ):
        if a == b or b in a.parents:
            raise SkillImportError(f"{message}: {src}")


def _assert_no_conflict(dest: Path, name: str, destination_root: Path) -> None:
    """Refuse a name already taken, by a skill or by anything else.

    A same-named entry in another scope is not a conflict: the two are shown
    side by side with their own sources, and the existing discovery order
    decides which one wins.
    """
    # ``lexists`` rather than ``exists``: a dangling symlink or a plain file
    # under this name is just as much in the way as a directory.
    if os.path.lexists(dest):
        raise SkillConflictError(f"已存在同名目录: {dest}")
    for entry in _iterdir(destination_root):
        try:
            other = load_skill(entry, "project")
        except Exception:  # noqa: BLE001 - an unreadable neighbour holds no name
            other = None
        if other is not None and other.name == name:
            raise SkillConflictError(f"已安装的 {entry.name} 使用了同一个命令名: {name}")


def _iterdir(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    try:
        return sorted(path.iterdir())
    except OSError as exc:
        raise SkillImportError(f"无法读取 {path}: {exc}") from exc


@dataclass
class _Budget:
    """What one import is allowed to copy, counted as it goes."""

    entries: int = 0
    size: int = 0


def _identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino)


def _copy_tree(src: Path, dest: Path) -> None:
    _copy_dir(src, dest, _Budget(), 0, ())


def _copy_dir(
    src: Path,
    dest: Path,
    budget: _Budget,
    depth: int,
    ancestors: tuple[tuple[int, int], ...],
) -> None:
    """Copy one directory, refusing links that lead back into the package.

    ``ancestors`` holds the resolved identity of every directory above this one
    on the walk: a link resolving to one of them is a cycle, while the same
    directory reached twice by different links is not (it simply gets copied
    twice, which is what "resolve the link" means for a package).
    """
    if depth > MAX_DEPTH:
        raise SkillImportError(f"目录层级超过上限（{MAX_DEPTH} 层）: {src}")
    key = _identity(src)
    if key in ancestors:
        raise SkillImportError(f"符号链接形成环: {src}")
    here = (*ancestors, key)
    dest.mkdir()
    for entry in _iterdir(src):
        budget.entries += 1
        if budget.entries > MAX_ENTRIES:
            raise SkillImportError(f"条目数超过上限（{MAX_ENTRIES} 项）: {src}")
        target = dest / entry.name
        if entry.is_symlink():
            # A link inside the package is a way of shipping the same file
            # twice; the copy holds the real content, so it survives the
            # source being moved. Links out of the package are refused: the
            # import must not become a way of copying an arbitrary file.
            resolved = _resolve_link(entry, src)
            if resolved.is_dir():
                _copy_dir(resolved, target, budget, depth + 1, here)
            elif resolved.is_file():
                _copy_file(resolved, target, budget)
            else:
                raise SkillImportError(f"不支持的文件类型: {entry}")
        elif entry.is_dir():
            _copy_dir(entry, target, budget, depth + 1, here)
        elif entry.is_file():
            _copy_file(entry, target, budget)
        else:
            raise SkillImportError(f"不支持的文件类型（FIFO/socket/设备文件）: {entry}")


def _resolve_link(link: Path, root: Path) -> Path:
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillImportError(f"损坏或成环的符号链接: {link}（{exc}）") from exc
    if target != root and root not in target.parents:
        raise SkillImportError(f"符号链接指向源文件夹之外: {link} -> {target}")
    return target


def _copy_file(src: Path, dest: Path, budget: _Budget) -> None:
    try:
        size = src.stat().st_size
    except OSError as exc:
        raise SkillImportError(f"无法读取 {src}: {exc}") from exc
    budget.size += size
    if budget.size > MAX_BYTES:
        raise SkillImportError(f"文件总量超过上限（{MAX_BYTES // (1024 * 1024)} MiB）: {src}")
    try:
        # copy2 keeps the mode, so an executable script stays executable.
        shutil.copy2(src, dest)
    except OSError as exc:
        raise SkillImportError(f"复制失败 {src}: {exc}") from exc


def _rename_exclusive(src: Path, dest: Path) -> None:
    """Publish ``src`` as ``dest``, failing if ``dest`` already exists.

    ``os.rename`` would silently replace an existing *empty* directory, so the
    platform's no-replace variant is used instead. Where neither exists the
    import is refused rather than downgraded to a replace: publishing over a
    directory the user already had is the one outcome this must never risk.
    """
    if sys.platform == "darwin":
        _rename_atomic(src, dest, "renamex_np", _RENAME_EXCL)
        return
    if sys.platform.startswith("linux"):
        _rename_atomic(src, dest, "renameat2", _RENAME_NOREPLACE)
        return
    raise SkillImportError(f"当前平台不支持不覆盖的原子发布: {sys.platform}")


_RENAME_EXCL = 0x00000004  # macOS renamex_np
_RENAME_NOREPLACE = 1  # Linux renameat2
_AT_FDCWD = -100


def _rename_atomic(src: Path, dest: Path, symbol: str, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, symbol, None)
    if fn is None:
        raise SkillImportError(f"当前系统缺少 {symbol}，无法做不覆盖的原子发布")
    fn.restype = ctypes.c_int
    if symbol == "renamex_np":
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(src), os.fsencode(dest), ctypes.c_uint(flags))
    else:
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        args = (
            ctypes.c_int(_AT_FDCWD),
            os.fsencode(src),
            ctypes.c_int(_AT_FDCWD),
            os.fsencode(dest),
            ctypes.c_uint(flags),
        )
    ctypes.set_errno(0)
    result = fn(*args)
    if result == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise SkillConflictError(f"已存在同名目录: {dest}")
    raise SkillImportError(f"发布到 {dest} 失败: {os.strerror(err)}")
