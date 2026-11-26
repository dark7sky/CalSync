"""Menlo Security 웹 격리를 경유해 CalSync 서버로 일정을 전송한다.

이 PC에서 나가는 POST는 Menlo가 200 HTML 격리 페이지로 대체하므로 직접 전송이
불가능하다. Menlo 셸이 렌더링한 CalSync 화면을 시스템 Edge로 조작하면 폼 제출은
Menlo 클라우드 브라우저에서 발생해 서버에 도달한다. GET은 격리되지 않으므로
세션 확인·영수증은 화면이 아닌 HTTP로도 가능하지만, 쿠키가 Menlo 세션에 있으므로
화면 경로를 단일 구현으로 사용한다.
"""
from __future__ import annotations

import json
import time
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

LOGIN_FAILURE_MARKERS = (
    "올바르지 않습니다",
    "Unexpected Origin",
    "Too many login attempts",
    "Login required",
)


class MenloError(RuntimeError):
    """Menlo 경유 전송 실패."""


class MenloTransport:
    def __init__(
        self,
        origin: str,
        *,
        headless: bool = False,
        timeout_ms: int = 90_000,
        channel: str = "msedge",
    ) -> None:
        self.origin = origin.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.channel = channel
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "MenloTransport":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                channel=self.channel,
                headless=self.headless,
                args=["--no-first-run", "--disable-features=msEdgeSidebarV2"],
            )
        except PlaywrightError as exc:
            self._shutdown()
            raise MenloError(f"Edge 실행 실패: {exc}") from exc
        self._context = self._browser.new_context(locale="ko-KR")
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def close(self) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        for attr in ("_browser",):
            target = getattr(self, attr, None)
            if target is not None:
                try:
                    target.close()
                except PlaywrightError:
                    pass
                setattr(self, attr, None)
        if self._pw is not None:
            try:
                self._pw.stop()
            except PlaywrightError:
                pass
            self._pw = None
        self._context = None
        self._page = None

    @property
    def page(self):
        if self._page is None:
            raise MenloError("Menlo 브라우저가 시작되지 않았습니다")
        return self._page

    def _visit(self, path: str, ready: str) -> None:
        """격리 셸이 화면을 다 그릴 때까지 재시도하며 연다."""
        last_error: Exception | None = None
        for attempt in range(3):
            url = f"{self.origin}{path}"
            if attempt:
                url += f"{'&' if '?' in path else '?'}retry={attempt}-{int(time.time())}"
            try:
                self.page.goto(url, wait_until="load", timeout=self.timeout_ms)
                self.page.get_by_role("textbox", name=ready).wait_for(
                    state="visible", timeout=45_000
                )
                return
            except PlaywrightError as exc:
                last_error = exc
                time.sleep(2)
        raise MenloError(f"화면 로드 실패({path}): {last_error}") from last_error

    def _visit_dashboard(self) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            url = f"{self.origin}/"
            if attempt:
                url += f"?retry={attempt}-{int(time.time())}"
            try:
                self.page.goto(url, wait_until="load", timeout=self.timeout_ms)
                self.page.locator("#importText").wait_for(state="visible", timeout=45_000)
                return
            except PlaywrightError as exc:
                last_error = exc
                time.sleep(2)
        raise MenloError(f"로그인 화면 진입 실패: {last_error}") from last_error

    def _body(self) -> str:
        try:
            return self.page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            ) or ""
        except PlaywrightError:
            return ""

    def _dashboard_visible(self) -> bool:
        try:
            area = self.page.locator("#importText")
            return bool(area.count()) and area.is_visible()
        except PlaywrightError:
            return False

    def _login_form_visible(self) -> bool:
        try:
            box = self.page.get_by_role("textbox", name="사용자 이름")
            return bool(box.count()) and box.is_visible()
        except PlaywrightError:
            return False

    def _fill(self, label: str, value: str) -> None:
        box = self.page.get_by_role("textbox", name=label)
        box.click()
        self.page.keyboard.insert_text(value)
        time.sleep(0.4)

    def _await_login_result(self, timeout_s: float = 90) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._dashboard_visible():
                return True
            body = self._body()
            for marker in LOGIN_FAILURE_MARKERS:
                if marker in body:
                    raise MenloError(f"로그인 실패: {marker}")
            time.sleep(1)
        return False

    def login(self, username: str, password: str) -> None:
        self._visit("/login", "사용자 이름")
        self._fill("사용자 이름", username)
        self._fill("비밀번호", password)

        # Menlo toast가 첫 클릭을 삼키거나 간헐적으로 전달 안 되는 경우를 보완해
        # 클릭/Enter 폼 제출을 번갈아 재시도한다. 진짜 비밀번호 오류면 즉시 raise.
        for submit in ("click", "enter", "click", "enter"):
            try:
                if submit == "enter":
                    self.page.get_by_role("textbox", name="비밀번호").press("Enter")
                else:
                    self.page.get_by_role("button", name="로그인").click()
            except PlaywrightError:
                continue
            if self._await_login_result(timeout_s=12):
                return
            time.sleep(1)

        self._visit_dashboard()
        if self._dashboard_visible():
            return
        if self._login_form_visible():
            raise MenloError("로그인 실패: 자격 증명이 거부되었습니다")
        raise MenloError("로그인 결과를 확인하지 못했습니다(Menlo 응답 지연)")

    def ensure_logged_in(self, username: str, password: str) -> None:
        """persistent 창 재사용 진입점. 로그인돼 있으면 그대로, 아니면 login()으로 진행."""
        self.page.goto(f"{self.origin}/", wait_until="load", timeout=self.timeout_ms)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._dashboard_visible():
                return
            if self._login_form_visible():
                self.login(username, password)
                self._visit_dashboard()
                return
            time.sleep(1)
        self._visit_dashboard()

    def submit_json(self, payload: dict[str, Any]) -> str:
        self._visit_dashboard()
        area = self.page.locator("#importText")
        area.click()
        self.page.keyboard.insert_text(json.dumps(payload, ensure_ascii=False))
        time.sleep(0.5)
        self.page.locator("#sendBatch").click()

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            message = self.page.locator("#message")
            try:
                text = message.inner_text(timeout=3_000)
                classes = message.get_attribute("class") or ""
            except PlaywrightError:
                text, classes = "", ""
            if "error" in classes:
                raise MenloError(f"전송 거부: {text}")
            if "success" in classes or "접수했습니다" in text or "등록했습니다" in text:
                return text
            time.sleep(1)
        raise MenloError("전송 결과를 확인하지 못했습니다")

    def receipts(self) -> dict[str, Any]:
        self._visit_dashboard()
        # 배포 프론트엔드에 따라 #exportReceipts 버튼이 없을 수 있다. 세션 쿠키가
        # 있는 Menlo 페이지 안에서 fetch로 영수증을 직접 가져오는 경로를 먼저 시도한다.
        try:
            data = self.page.evaluate(
                "async () => {"
                "  const r = await fetch('/api/v1/receipts',"
                "    {credentials:'same-origin', cache:'no-store'});"
                "  return {ok:r.ok, body: await r.text()};"
                "}"
            )
            if data and data.get("ok"):
                return json.loads(data["body"])
        except (PlaywrightError, json.JSONDecodeError):
            pass

        try:
            self.page.locator("#exportReceipts").click(timeout=20_000)
        except PlaywrightError:
            raise MenloError("영수증을 받지 못했습니다(버튼과 fetch 모두 실패)") from None
        field = self.page.locator("#receiptText")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                value = field.input_value(timeout=3_000)
            except PlaywrightError:
                value = ""
            if value:
                return json.loads(value)
            time.sleep(1)
        raise MenloError("영수증을 받지 못했습니다")
