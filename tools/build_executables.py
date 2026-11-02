#!/usr/bin/env python3
"""Build the two standalone packages of the Shelter console.

  dist/ShelterConsole-Mac.zip      PyInstaller one-file executable, universal2 (Intel + Apple)
  dist/ShelterConsole-Windows.zip  Windows embeddable Python + the console, started by a .bat

Neither needs Python on the target machine. The AI relay settings from ../.env are
baked in as shelter.key. That file is only obfuscated: anyone with a package can
recover the key, so share the packages with the team only.

Usage:
  python3 tools/build_executables.py --pyinstaller <path to pyinstaller from a universal2 Python>
                                     --windows-embed <python-3.x-embed-amd64.zip from python.org>
"""
import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
DIST = ROOT / "dist"
sys.path.insert(0, str(APP))
from shelter_console import encode_bundle  # noqa: E402

RESOURCES = ["console.html", "sample.jpg"]


def ai_settings():
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL") and value.strip():
            values[key.strip()] = value.strip()
    if not values.get("OPENAI_API_KEY"):
        sys.exit(".env 里没有 OPENAI_API_KEY")
    return values


def zip_folder(folder: Path, archive: Path):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo.from_file(path, Path(folder.name) / path.relative_to(folder))
                if path.stat().st_mode & 0o111:
                    info.external_attr = 0o100755 << 16  # keep executables runnable after unzip
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, path.read_bytes())
    print(f"  {archive.name}: {archive.stat().st_size // 1024} KB")


def stage(key_text):
    folder = DIST / ".stage"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for name in ["shelter_console.py"] + RESOURCES:
        shutil.copy2(APP / name, folder / name)
    shutil.copytree(APP / "vendor", folder / "vendor", ignore=shutil.ignore_patterns("__pycache__"))
    (folder / "shelter.key").write_text(key_text)
    return folder


def build_mac(pyinstaller, staged, readme):
    out = DIST / "ShelterConsole-Mac"
    if out.exists():
        shutil.rmtree(out)
    work = DIST / ".pyinstaller"
    sep = ":"
    cmd = [pyinstaller, "--noconfirm", "--clean", "--onefile", "--console", "--name", "ShelterConsole",
           "--target-arch", "universal2", "--distpath", str(out), "--workpath", str(work),
           "--specpath", str(work), "--paths", str(staged / "vendor"),
           "--hidden-import", "certifi", "--collect-data", "certifi"]
    for name in RESOURCES + ["shelter.key"]:
        cmd += ["--add-data", f"{staged / name}{sep}."]
    cmd.append(str(staged / "shelter_console.py"))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    launcher = out / "ShelterConsole 模拟彩排.command"
    launcher.write_text('#!/bin/bash\n"$(dirname "$0")/ShelterConsole" --simulate\n')
    launcher.chmod(0o755)
    shutil.copy2(readme, out / "使用说明.txt")
    zip_folder(out, DIST / "ShelterConsole-Mac.zip")


def build_windows(embed_zip, staged, readme):
    out = DIST / "ShelterConsole-Windows"
    if out.exists():
        shutil.rmtree(out)
    (out / "runtime").mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(out / "runtime")
    for name in ["shelter_console.py", "shelter.key"] + RESOURCES:
        shutil.copy2(staged / name, out / name)
    shutil.copytree(staged / "vendor", out / "vendor")
    bat = '@echo off\r\ncd /d "%~dp0"\r\n"runtime\\python.exe" shelter_console.py {args}\r\npause\r\n'
    for name, extra in (("Start Shelter.bat", "%*"), ("Start Shelter (simulate).bat", "--simulate")):
        (out / name).write_bytes(bat.format(args=extra).encode("ascii"))
    text = readme.read_text(encoding="utf-8").replace("\n", "\r\n")
    (out / "使用说明.txt").write_bytes(text.encode("utf-8-sig"))
    zip_folder(out, DIST / "ShelterConsole-Windows.zip")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pyinstaller", help="pyinstaller executable from a universal2 Python")
    parser.add_argument("--windows-embed", type=Path, help="python-3.x-embed-amd64.zip")
    args = parser.parse_args()
    if not args.pyinstaller and not args.windows_embed:
        parser.error("give --pyinstaller and/or --windows-embed")
    staged = stage(encode_bundle(ai_settings()))
    readme = APP / "README_使用说明.md"
    print("构建：")
    if args.pyinstaller:
        build_mac(args.pyinstaller, staged, readme)
    if args.windows_embed:
        build_windows(args.windows_embed, staged, readme)
    shutil.rmtree(staged)
    shutil.rmtree(DIST / ".pyinstaller", ignore_errors=True)
    print("完成。压缩包里有内置的 AI 密钥：只发给组员。")


if __name__ == "__main__":
    main()
