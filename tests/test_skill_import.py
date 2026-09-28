"""Skill import: validation, copying and atomic publication."""

from __future__ import annotations

import os
import stat
import sys

import pytest

import easycode.extensions.skill_import as skill_import
from easycode.extensions.skill_import import (
    MAX_BYTES,
    MAX_DEPTH,
    MAX_ENTRIES,
    SkillConflictError,
    SkillImportError,
    import_skill,
)
from easycode.extensions.skills import load_skill


def make_skill(root, name: str = "pack", body: str = "Follow the steps") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test pack\n---\n{body}\n", encoding="utf-8"
    )


def test_import_copies_the_whole_package(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    (src / "pack" / "references").mkdir()
    (src / "pack" / "references" / "guide.md").write_text("guide\n", encoding="utf-8")
    (src / "pack" / "scripts").mkdir()
    script = src / "pack" / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    dest_root = tmp_path / "proj" / ".easycode" / "skills"

    installed = import_skill(src / "pack", dest_root, "project")

    installed_dir = dest_root / "pack"
    assert installed.directory == installed_dir
    assert installed.source == "project"
    assert (installed_dir / "references" / "guide.md").read_text(encoding="utf-8") == "guide\n"
    # The script keeps its executable bit: a package that could not run its own
    # script after installation would have been copied, not installed.
    assert os.stat(installed_dir / "scripts" / "run.sh").st_mode & stat.S_IXUSR
    # No staging directory survives a successful import.
    assert not [p for p in (tmp_path / "proj" / ".easycode").iterdir() if p.name.startswith(".skill-import-")]


def test_import_reads_back_from_the_installed_copy(tmp_path):
    """The installed skill must not keep pointing at where it came from."""
    src = tmp_path / "downloads"
    make_skill(src)
    dest_root = tmp_path / "proj" / ".easycode" / "skills"

    installed = import_skill(src / "pack", dest_root, "project")
    assert installed.path is not None
    assert installed.path.is_relative_to(dest_root)

    # The source is gone; the installed copy still loads.
    for path in sorted((src / "pack").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (src / "pack").rmdir()
    assert load_skill(dest_root / "pack", "project").name == "pack"


def test_personal_scope_marks_the_skill_as_personal(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    installed = import_skill(src / "pack", tmp_path / "data" / "skills", "personal")
    assert installed.source == "user"


def test_symlinked_file_and_directory_inside_the_package_are_copied_through(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    real = src / "pack" / "real"
    real.mkdir()
    (real / "deep.txt").write_text("deep\n", encoding="utf-8")
    (src / "pack" / "alias-dir").symlink_to(real)
    (src / "pack" / "alias-file").symlink_to(real / "deep.txt")

    installed = import_skill(src / "pack", tmp_path / "skills", "project")
    dest = tmp_path / "skills" / "pack"
    assert not (dest / "alias-dir").is_symlink()
    assert (dest / "alias-dir" / "deep.txt").read_text(encoding="utf-8") == "deep\n"
    assert not (dest / "alias-file").is_symlink()
    assert (dest / "alias-file").read_text(encoding="utf-8") == "deep\n"
    assert installed.directory == dest


def test_link_out_of_the_package_is_refused(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    src = tmp_path / "src"
    make_skill(src)
    (src / "pack" / "escape").symlink_to(outside)

    with pytest.raises(SkillImportError, match="源文件夹之外"):
        import_skill(src / "pack", tmp_path / "skills", "project")
    assert not (tmp_path / "skills" / "pack").exists()


def test_broken_link_is_refused(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    (src / "pack" / "dangling").symlink_to(src / "pack" / "nowhere")

    with pytest.raises(SkillImportError, match="符号链接"):
        import_skill(src / "pack", tmp_path / "skills", "project")


def test_link_cycle_is_refused(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    # A link pointing back at the package root is a cycle, not a copy.
    (src / "pack" / "loop").symlink_to(src / "pack")

    with pytest.raises(SkillImportError, match="环"):
        import_skill(src / "pack", tmp_path / "skills", "project")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_special_file_is_refused(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    os.mkfifo(src / "pack" / "pipe")

    with pytest.raises(SkillImportError, match="不支持的文件类型"):
        import_skill(src / "pack", tmp_path / "skills", "project")


def test_entry_limit_is_enforced(tmp_path, monkeypatch):
    src = tmp_path / "src"
    make_skill(src)
    for i in range(5):
        (src / "pack" / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("easycode.extensions.skill_import.MAX_ENTRIES", 3)

    with pytest.raises(SkillImportError, match=str(3)):
        import_skill(src / "pack", tmp_path / "skills", "project")
    assert not (tmp_path / "skills" / "pack").exists()
    # The staging directory does not outlive the failure.
    assert not [p for p in (tmp_path / "skills").iterdir() if p.name.startswith(".skill-import-")]


def test_byte_limit_is_enforced(tmp_path, monkeypatch):
    src = tmp_path / "src"
    make_skill(src)
    (src / "pack" / "big.bin").write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr("easycode.extensions.skill_import.MAX_BYTES", 32)

    with pytest.raises(SkillImportError, match="MiB"):
        import_skill(src / "pack", tmp_path / "skills", "project")


def test_depth_limit_is_enforced(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    deep = src / "pack"
    for _ in range(MAX_DEPTH + 2):
        deep = deep / "d"
    deep.mkdir(parents=True)

    with pytest.raises(SkillImportError, match="层级"):
        import_skill(src / "pack", tmp_path / "skills", "project")


def test_source_inside_the_destination_is_refused(tmp_path):
    dest_root = tmp_path / "proj" / ".easycode" / "skills"
    make_skill(dest_root)

    with pytest.raises(SkillImportError, match="复制自身"):
        import_skill(dest_root / "pack", dest_root, "project")


def test_destination_inside_the_source_is_refused(tmp_path):
    src = tmp_path / "src"
    make_skill(src)

    with pytest.raises(SkillImportError, match="复制自身"):
        import_skill(src / "pack", src / "pack" / "nested", "project")


def test_same_directory_name_conflicts(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    dest_root = tmp_path / "skills"
    (dest_root / "pack").mkdir(parents=True)
    (dest_root / "pack" / "junk.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SkillConflictError, match="同名目录"):
        import_skill(src / "pack", dest_root, "project")
    # What was there is untouched: a conflict never replaces anything.
    assert (dest_root / "pack" / "junk.txt").read_text(encoding="utf-8") == "x"


def test_same_command_name_conflicts(tmp_path):
    """A different folder name does not make a second copy of the same command."""
    src = tmp_path / "src"
    make_skill(src, "pack")
    dest_root = tmp_path / "skills"
    make_skill(dest_root, "other")  # same frontmatter name: pack
    (dest_root / "other" / "SKILL.md").write_text(
        "---\nname: pack\ndescription: other\n---\nbody\n", encoding="utf-8"
    )

    with pytest.raises(SkillConflictError, match="命令名"):
        import_skill(src / "pack", dest_root, "project")
    assert not (dest_root / "pack").exists()


def test_same_name_in_another_scope_is_allowed(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    personal = tmp_path / "data" / "skills"
    make_skill(personal)

    installed = import_skill(src / "pack", tmp_path / "proj" / ".easycode" / "skills", "project")
    assert installed.directory == tmp_path / "proj" / ".easycode" / "skills" / "pack"
    assert (personal / "pack" / "SKILL.md").is_file()


def test_missing_or_unusable_skill_md_is_refused(tmp_path):
    src = tmp_path / "src"
    (src / "plain").mkdir(parents=True)
    (src / "plain" / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    (src / "empty").mkdir()
    (src / "empty" / "notes.md").write_text("x", encoding="utf-8")

    with pytest.raises(SkillImportError, match="description"):
        import_skill(src / "plain", tmp_path / "skills", "project")
    with pytest.raises(SkillImportError, match=r"SKILL\.md"):
        import_skill(src / "empty", tmp_path / "skills", "project")
    with pytest.raises(SkillImportError, match="不是文件夹"):
        import_skill(src / "empty" / "notes.md", tmp_path / "skills", "project")
    with pytest.raises(SkillImportError, match="无法读取源文件夹"):
        import_skill(src / "missing", tmp_path / "skills", "project")


@pytest.mark.parametrize("name", ["mcp:demo", "two words", "a/b", "a\\b"])
def test_reserved_and_unusable_names_are_refused(tmp_path, name):
    src = tmp_path / "src"
    make_skill(src)
    (src / "pack" / "SKILL.md").write_text(
        f"---\nname: '{name}'\ndescription: bad\n---\nbody\n", encoding="utf-8"
    )

    with pytest.raises(SkillImportError):
        import_skill(src / "pack", tmp_path / "skills", "project")


def test_symlinked_install_root_is_refused(tmp_path):
    src = tmp_path / "src"
    make_skill(src)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "proj" / ".easycode"
    root.mkdir(parents=True)
    (root / "skills").symlink_to(elsewhere)

    with pytest.raises(SkillImportError, match="符号链接"):
        import_skill(src / "pack", root / "skills", "project")
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="needs a no-replace rename")
def test_publish_does_not_replace_an_existing_empty_directory(tmp_path, monkeypatch):
    """The last check before publishing is the rename itself."""
    src = tmp_path / "src"
    make_skill(src)
    dest_root = tmp_path / "skills"

    # A name that appears only between the last check and the rename models the
    # real race: another import landing while this one copies. Skipping the
    # second check leaves the rename as the only thing that can refuse it.
    calls = {"n": 0}
    original = skill_import._assert_no_conflict

    def racy(dest, name, root):
        calls["n"] += 1
        if calls["n"] > 1:
            return
        original(dest, name, root)
        dest.mkdir(parents=True)
        (dest / "theirs.txt").write_text("theirs", encoding="utf-8")

    monkeypatch.setattr(skill_import, "_assert_no_conflict", racy)
    with pytest.raises(SkillConflictError):
        import_skill(src / "pack", dest_root, "project")

    # The directory that won keeps its own contents, and no staging is left.
    assert (dest_root / "pack" / "theirs.txt").read_text(encoding="utf-8") == "theirs"
    assert not [p for p in dest_root.iterdir() if p.name.startswith(".skill-import-")]


def test_limits_are_the_documented_ones():
    assert (MAX_ENTRIES, MAX_BYTES, MAX_DEPTH) == (10_000, 100 * 1024 * 1024, 64)
