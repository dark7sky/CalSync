"""Windows 자격 증명 관리자에 CalSync 비밀번호를 보관한다.

비밀번호는 Windows Credential Locker(keyring WinVaultKeyring)에만 저장되고
설정 파일·로그·명령줄 인자에는 남기지 않는다.
"""
from __future__ import annotations

import argparse
import getpass
import sys

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "CalSync"


def load(username: str) -> str | None:
    return keyring.get_password(SERVICE, username)


def save(username: str, password: str) -> None:
    keyring.set_password(SERVICE, username, password)


def delete(username: str) -> bool:
    try:
        keyring.delete_password(SERVICE, username)
    except PasswordDeleteError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="credentials", description="CalSync 비밀번호 보관")
    parser.add_argument("action", choices=["set", "status", "delete"])
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    if args.action == "set":
        password = getpass.getpass("비밀번호: ")
        if not password:
            print("비어 있는 비밀번호는 저장하지 않습니다.", file=sys.stderr)
            return 2
        if not password.isascii():
            # 한글 IME가 켜진 채 입력하면 자모가 그대로 저장돼(yys->ㅛㅛㄴ) 로그인이 거부된다.
            print(
                "경고: 비밀번호에 ASCII가 아닌 문자(한글 자모 등)가 포함되어 있습니다.",
                file=sys.stderr,
            )
            print(
                "  가능성: 한글 IME가 켜진 채 입력(예: yys -> ㅛㅛㄴ). 영문 입력으로 바꾼 뒤 재시도하세요.",
                file=sys.stderr,
            )
            confirm = input("그대로 저장하려면 yes 를 입력하세요: ").strip().lower()
            if confirm != "yes":
                print("저장을 취소했습니다.", file=sys.stderr)
                return 3
        save(args.username, password)
        print(f"저장 완료: {SERVICE}\\{args.username}")
        return 0
    if args.action == "status":
        print("저장됨" if load(args.username) else "없음")
        return 0
    print("삭제됨" if delete(args.username) else "저장된 항목 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
