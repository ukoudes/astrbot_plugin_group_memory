"""Build a minimal, audited AstrBot installation archive from an allowlist."""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT.name
FILES = (
    "__init__.py",
    "_conf_schema.json",
    "auto.py",
    "core.py",
    "history_backfill.py",
    "logo.png",
    "mailer.py",
    "main.py",
    "media.py",
    "metadata.yaml",
    "report_policy.py",
    "settings.py",
    "store.py",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "pages/auto-summary/app.js",
    "pages/auto-summary/index.html",
    "pages/auto-summary/settings.js",
    "pages/auto-summary/style.css",
)
TEXT_SUFFIXES = {".py", ".json", ".yaml", ".md", ".js", ".html", ".css"}
EMAIL = re.compile(r"(?<![\w@])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w@])", re.I)
LONG_NUMBER = re.compile(r"(?<!\d)\d{9,12}(?!\d)")
LOCAL_PATH = re.compile(r"/(?:Users|home|var/folders)/[^\s\x22\x27<>]+")
TOKEN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
ALLOWED_NUMBERS = {"123456789", "4102444800"}
TEST_EMAILS = {
    f"{local}@{domain}" for local, domain in (
        ("bot", "qq.com"), ("other", "qq.com"),
        ("multimedia.nt.qq.com.cn", "evil.com"),
        ("pass", "gchat.qpic.cn"),
    )
}


def version() -> str:
    match = re.search(r"^version:\s*['\x22]?(v\d+\.\d+\.\d+)", (ROOT / "metadata.yaml").read_text(), re.M)
    if not match:
        raise ValueError("metadata.yaml 缺少有效版本号")
    return match.group(1)


def audit_text(path: str, data: bytes, *, tests: bool = False) -> list[str]:
    if Path(path).suffix not in TEXT_SUFFIXES:
        return []
    text = data.decode("utf-8")
    issues = []
    if LOCAL_PATH.search(text):
        issues.append("本机绝对路径")
    if TOKEN.search(text):
        issues.append("疑似密钥")
    for email in EMAIL.findall(text):
        value = email.lower()
        if not (value.endswith("@example.com") or value == "licensing@fsf.org"
                or (tests and value in TEST_EMAILS)):
            issues.append("非示例邮箱")
            break
    if any(number not in ALLOWED_NUMBERS for number in LONG_NUMBER.findall(text)):
        issues.append("疑似真实 QQ 号码")
    return issues


def audit_repository() -> list[str]:
    problems = []
    for source in ROOT.rglob("*"):
        if not source.is_file() or source.suffix not in TEXT_SUFFIXES:
            continue
        relative = source.relative_to(ROOT)
        if any(part in {".git", "dist", "__pycache__"} for part in relative.parts):
            continue
        try:
            data = source.read_bytes()
            issues = audit_text(relative.as_posix(), data,
                                tests=relative.parts[0] == "tests")
        except UnicodeError:
            problems.append(f"{relative}: 非 UTF-8 文本")
            continue
        problems.extend(f"{relative}: {issue}" for issue in issues)
    return problems


def build_bytes() -> bytes:
    problems = audit_repository()
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            source = ROOT / name
            if not source.is_file() or source.is_symlink():
                problems.append(f"{name}: 文件不存在或为符号链接")
                continue
            data = source.read_bytes()
            problems.extend(f"{name}: {issue}" for issue in audit_text(name, data))
            entry = zipfile.ZipInfo(f"{PACKAGE}/{name}", date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    if problems:
        raise ValueError("发布检查失败：\n" + "\n".join(problems))
    result = content.getvalue()
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        expected = {f"{PACKAGE}/{name}" for name in FILES}
        if set(archive.namelist()) != expected or archive.testzip() is not None:
            raise ValueError("安装包文件清单或 CRC 检查失败")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查，不写入安装包")
    args = parser.parse_args()
    try:
        data = build_bytes()
        if args.check:
            print(f"发布检查通过：{len(FILES)} 个指定文件")
        else:
            destination = ROOT / "dist" / f"{PACKAGE}-{version()}.zip"
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(data)
            print(destination)
        return 0
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
