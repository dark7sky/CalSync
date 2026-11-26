"""CalSync Windows GUI and system-tray application.

전송은 Menlo 격리를 통과해야 하므로 시스템 Edge를 조작하는 menlo 모듈을 사용한다.
비밀번호는 Windows 자격 증명 관리자에 보관한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
import os

from PySide6.QtCore import QFileSystemWatcher, QSettings, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
)

from core import Engine, json_compact
from credentials import load as load_password
from credentials import save as save_password
from menlo import MenloError, MenloTransport


def data_folder() -> Path:
    folder = Path.home() / "YYS_butler" / "CalSync"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent)
    return str(Path(base) / relative)


class Client(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CalSync - Windows Desktop")
        self.setMinimumSize(920, 640)
        self.config = QSettings(str(data_folder() / "settings.ini"), QSettings.Format.IniFormat)
        self.engine = Engine(data_folder() / "client.db")
        self.monitoring = False
        self.exiting = False
        self.transport = None              # Menlo 브라우저 1개를 재사용
        self.last_batch = None
        self.password = None
        self.auto_send_hinted = False
        self.auto_send_blocked_hinted = False
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self.schedule_scan)
        self.watcher.fileChanged.connect(self.schedule_scan)

        self.tray = QSystemTrayIcon(self)
        icon = QIcon(resource_path("assets/CalSync.ico"))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)
        menu = QMenu(self)
        self.tray_sync = QAction("지금동기화", self)
        self.tray_sync.triggered.connect(self.sync_now)
        self.tray_log = QAction("로그보기", self)
        self.tray_log.triggered.connect(self.show_log)
        self.tray_restore = QAction("창복원", self)
        self.tray_restore.triggered.connect(self.restore)
        self.tray_exit = QAction("종료", self)
        self.tray_exit.triggered.connect(self.quit)
        menu.addActions([self.tray_sync, self.tray_log, self.tray_restore])
        menu.addSeparator()
        menu.addAction(self.tray_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.restore()
            if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                          QSystemTrayIcon.ActivationReason.Trigger) else None
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        host = QWidget()
        self.setCentralWidget(host)
        vertical = QVBoxLayout(host)

        self.status = QLabel("로컬 감시 중지")
        vertical.addWidget(self.status)
        info = QLabel("직접 전송은 사내 프록시 NTLM SSO로 work 도메인에 접속합니다. "
                      "비밀번호는 저장되지 않고 세션 메모리에만 유지됩니다.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#64748b")
        vertical.addWidget(info)

        config_group = QGroupBox("설정")
        form = QFormLayout(config_group)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(self.config.value("folder", str(Path.home() / "YYS_butler")))
        browse = QPushButton("폴더 선택")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder)
        folder_row.addWidget(browse)
        form.addRow("JSON 폴더", folder_row)
        self.url = QLineEdit(self.config.value("url", "https://calendar.example.com"))
        form.addRow("CalSync HTTPS URL", self.url)
        self.server_username = QLineEdit(self.config.value("server_username", ""))
        self.server_username.setPlaceholderText("예: sender1 (비밀번호는 전송 시에만 입력)")
        form.addRow("CalSync 계정", self.server_username)
        self.deletions = QLineEdit(self.config.value("deletion_types", ""))
        self.deletions.setPlaceholderText("예: cancelled,deleted (실제 JSON type 값 확인 후 설정)")
        form.addRow("명시적 삭제 type 값", self.deletions)
        self.autostart = QCheckBox("Windows 로그인 시 자동 실행 (시스템 트레이)")
        self.autostart.setChecked(self.check_autostart())
        self.autostart.toggled.connect(self.set_autostart)
        form.addRow("로그인 자동 실행", self.autostart)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setValue(int(self.config.value("interval", 10)))
        self.interval.setSuffix("분")
        form.addRow("주기적 재스캔", self.interval)
        self.auto_send = QCheckBox("새 항목 감지 시 서버 자동 전송 (비밀번호 1회 입력 후 세션 유지)")
        self.auto_send.setChecked(str(self.config.value("auto_send", "false")).lower() == "true")
        self.auto_send.toggled.connect(self.save_settings)
        form.addRow("자동 전송", self.auto_send)
        vertical.addWidget(config_group)

        buttons = QHBoxLayout()
        self.monitor_button = QPushButton("로컬 자동 감시 시작")
        self.monitor_button.clicked.connect(self.toggle_monitor)
        self.scan_button = QPushButton("지금 스캔")
        self.scan_button.clicked.connect(self.scan)
        self.send_button = QPushButton("서버로 직접 전송")
        self.send_button.clicked.connect(self.send_direct)
        self.fetch_button = QPushButton("서버 영수증 자동 조회")
        self.fetch_button.clicked.connect(self.fetch_receipts)
        self.prepare_button = QPushButton("전송 데이터 준비 · 복사")
        self.prepare_button.clicked.connect(self.prepare)
        self.open_button = QPushButton("Menlo 웹페이지 열기")
        self.open_button.clicked.connect(self.open_site)
        for b in (self.monitor_button, self.scan_button, self.send_button,
                  self.fetch_button, self.prepare_button, self.open_button):
            buttons.addWidget(b)
        vertical.addLayout(buttons)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["번호", "동작", "상태", "원본 식별자", "Batch ID"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        vertical.addWidget(self.table)

        receipt_actions = QHBoxLayout()
        self.submitted_button = QPushButton("웹 제출 버튼을 눌렀음 (확인 전)")
        self.submitted_button.clicked.connect(self.mark_submitted)
        self.import_button = QPushButton("서버 영수증/상태 가져오기")
        self.import_button.clicked.connect(self.import_receipt)
        receipt_actions.addWidget(self.submitted_button)
        receipt_actions.addWidget(self.import_button)
        vertical.addLayout(receipt_actions)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(130)
        vertical.addWidget(self.logs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.scan)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.scan)
        self.folder.editingFinished.connect(self.folder_changed)
        self.interval.valueChanged.connect(self.reset_timer)
        self.url.editingFinished.connect(self.save_settings)
        self.server_username.editingFinished.connect(self.save_settings)
        self.deletions.editingFinished.connect(self.save_settings)

        self.refresh_table()
        if str(self.config.value("monitor", "false")).lower() == "true":
            self.start_monitor()

    def log(self, message: str):
        from datetime import datetime
        now = datetime.now()
        line = f"[{now:%H:%M:%S}] {message}"
        self.logs.append(line)
        try:
            with open(data_folder() / f"{now:%Y%m}.log", "a", encoding="utf-8") as fh:
                fh.write(f"{now:%Y-%m-%d} {line}\n")
        except OSError:
            pass

    def save_settings(self):
        self.config.setValue("folder", self.folder.text().strip())
        self.config.setValue("url", self.url.text().strip())
        self.config.setValue("server_username", self.server_username.text().strip())
        self.config.setValue("deletion_types", self.deletions.text())
        self.config.setValue("interval", self.interval.value())
        self.config.setValue("auto_send", "true" if self.auto_send.isChecked() else "false")
        self.config.setValue("monitor", "true" if self.monitoring else "false")
        self.config.sync()

    def check_autostart(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, "CalSyncWindows")
                return bool(value)
        except OSError:
            return False

    def set_autostart(self, enabled: bool):
        if sys.platform != "win32":
            QMessageBox.information(self, "Windows 전용", "로그인 자동 실행은 Windows에서 설정합니다.")
            return
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        command = (f'"{sys.executable}" --tray' if getattr(sys, "frozen", False)
                   else f'"{sys.executable}" "{Path(__file__).resolve()}" --tray')
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                if enabled:
                    winreg.SetValueEx(key, "CalSyncWindows", 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, "CalSyncWindows")
                    except FileNotFoundError:
                        pass
            self.log("로그인 자동 실행 " + ("활성화" if enabled else "비활성화"))
        except OSError as exc:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not enabled)
            self.autostart.blockSignals(False)
            QMessageBox.warning(self, "자동 실행 설정 실패", str(exc))

    def choose_folder(self):
        choice = QFileDialog.getExistingDirectory(
            self, "JSON 파일 폴더", self.folder.text())
        if choice:
            self.folder.setText(choice)
            self.folder_changed()

    def folder_changed(self):
        self.save_settings()
        self.update_watches()
        if self.monitoring:
            self.scan()

    def update_watches(self):
        all_paths = self.watcher.files() + self.watcher.directories()
        if all_paths:
            self.watcher.removePaths(all_paths)
        target = Path(self.folder.text())
        if target.is_dir():
            self.watcher.addPath(str(target))
            for path in target.glob("*.json"):
                self.watcher.addPath(str(path))

    def reset_timer(self):
        self.save_settings()
        if self.monitoring:
            self.timer.start(self.interval.value() * 60_000)

    def start_monitor(self):
        if not Path(self.folder.text().strip()).is_dir():
            QMessageBox.warning(self, "폴더 없음", "유효한 JSON 폴더를 먼저 선택하세요.")
            return
        self.monitoring = True
        self.save_settings()
        self.update_watches()
        self.timer.start(self.interval.value() * 60_000)
        self.monitor_button.setText("로컬 자동 감시 일시중지")
        self.scan()
        self.status.setText("로컬 JSON 자동 감시 중 (서버 전송은 Menlo에서 확인 필요)")
        self.log("자동 감시 시작")

    def toggle_monitor(self):
        if not self.monitoring:
            self.start_monitor()
        else:
            self.monitoring = False
            self.timer.stop()
            self.debounce.stop()
            self.save_settings()
            self.monitor_button.setText("로컬 자동 감시 시작")
            self.status.setText("로컬 JSON 자동 감시 중지")
            self.log("자동 감시 일시중지")

    def schedule_scan(self):
        if self.monitoring:
            self.debounce.start(750)

    def scan(self):
        target = Path(self.folder.text().strip())
        try:
            result = self.engine.scan(
                target, {x.strip() for x in self.deletions.text().split(",") if x.strip()})
            self.update_watches()
            self.status.setText(
                f"{'감시 중' if self.monitoring else '수동 스캔'} · "
                f"발견 {result['events']}건 · 준비/처리 중 {self.engine.pending_count()}건 · "
                f"수정 반영 {result.get('replaced', 0)}건")
            for error in result["errors"][:15]:
                self.log("확인 필요: " + error)
            if result.get("replaced"):
                self.log(f"수정된 이벤트 {result['replaced']}건을 반영했습니다 "
                         f"(기존 일정 삭제 요청 + 변경된 내용 신규 등록 요청 생성).")
            if result["changes"]:
                self.log(f"수정된 기존 이벤트 {result['changes']}건은 서버 응답 대기 중입니다. "
                         f"확정되면 다음 스캔에서 자동 반영합니다.")
            self.refresh_table()
            if (self.monitoring and self.auto_send.isChecked()
                    and self.engine.queued_count() > 0
                    and self.engine.prepared_count() == 0):
                if self.password is None:
                    if not self.auto_send_hinted:
                        self.auto_send_hinted = True
                        self.log("자동 전송: 비밀번호 미입력으로 대기 중. '서버로 직접 전송'을 한 번 실행하세요.")
                else:
                    self.send_direct(silent=True)
            elif (self.monitoring and self.auto_send.isChecked()
                  and self.engine.queued_count() > 0
                  and self.engine.prepared_count() > 0):
                if not self.auto_send_blocked_hinted:
                    self.auto_send_blocked_hinted = True
                    self.log("자동 전송 보류: 이전에 확정 못 한 배치가 남아 자동 재전송을 막았습니다. "
                             "'서버로 직접 전송'으로 직접 확인하세요.")
            elif (self.monitoring and self.engine.queued_count() == 0
                  and self.engine.unconfirmed_count() > 0):
                # 새로 보낼 건은 없지만 확정 안 된 건이 있으면 영수증을 갱신한다.
                self.fetch_receipts(silent=True)
        except (ValueError, OSError, Exception) as error:
            self.log("스캔 실패: " + str(error))
            self.status.setText("스캔 실패: " + str(error))

    def origin_url(self) -> str:
        origin = self.url.text().strip()
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("CalSync HTTPS URL이 올바르지 않습니다 (https://도메인 형식).")
        return origin

    def ensure_transport(self) -> MenloTransport:
        """Menlo 브라우저 1개를 열어 재사용한다. 없거나 서버가 바뀌면 새로 연다."""
        origin = self.origin_url()
        if self.transport is None or self.transport.origin != origin:
            self.teardown_transport()
            trans = MenloTransport(origin)
            trans.start()
            self.transport = trans
        return self.transport

    def teardown_transport(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001 - 종료 중 정리 실패는 무시
                pass
            self.transport = None

    def ensure_password(self) -> str | None:
        if self.password:
            return self.password
        username = self.server_username.text().strip()
        if not username:
            QMessageBox.warning(self, "계정 필요", "설정의 'CalSync 계정'에 사용자 이름을 입력하세요.")
            return None
        stored = load_password(username)
        if stored:
            self.password = stored
            return stored
        text, ok = QInputDialog.getText(
            self, "CalSync 계정",
            f"'{username}' 계정의 CalSync 비밀번호 (Windows 자격 증명 관리자에 저장됩니다):",
            QLineEdit.EchoMode.Password)
        if not ok or not text:
            return None
        save_password(username, text)
        self.password = text
        return text

    def send_direct(self, silent: bool = False):
        packet = None
        try:
            origin = self.origin_url()
            packet = self.engine.prepare()
            if not packet:
                if not silent:
                    QMessageBox.information(self, "전송할 항목 없음", "준비된 새 요청이 없습니다.")
                return
            username = self.server_username.text().strip()
            password = self.ensure_password()
            if not password:
                return
            self.save_settings()
            transport = self.ensure_transport()
            try:
                transport.ensure_logged_in(username, password)
                transport.submit_json(packet)
            except MenloError:
                self.teardown_transport()
                raise
            document = None
            try:
                document = transport.receipts()
            except MenloError:
                self.log("접수 후 영수증 조회에 실패했습니다. 이 서버가 영수증 기능을 지원하지 않을 수 있습니다.")
            count = self.engine.mark_submitted(packet["batchId"])
            self.last_batch = packet["batchId"]
            if document is not None:
                self.engine.load_receipt(json.dumps(document, ensure_ascii=False))
                rows = [
                    row for row in document.get("requests", [])
                    if row.get("batchId") == packet["batchId"]
                ]
                tally: dict[str, int] = {}
                for row in rows:
                    status = str(row.get("status") or "?").upper()
                    tally[status] = tally.get(status, 0) + 1
                summary = ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
                if rows and set(tally) == {"SUCCEEDED"}:
                    self.log(f"{packet['batchId'][:8]}: 서버 전송 완료 - 요청 {len(rows)}건 "
                             f"모두 Google 캘린더에 반영되었습니다.")
                    if not silent:
                        QMessageBox.information(
                            self, "전송 완료", f"Google 캘린더 반영 완료 ({len(rows)}건).")
                else:
                    note = ""
                    if "FAILED" in tally:
                        note = " 실패 건은 서버 관리자 기록에서 원인을 확인하세요."
                    self.log(f"{packet['batchId'][:8]}: 서버 전송 완료 - 요청 {len(rows)}건 "
                             f"({summary or '상태 미수신'}).{note}")
                    if not silent:
                        QMessageBox.information(
                            self, "전송 완료",
                            f"요청 {len(rows)}건 처리 결과: {summary or '상태 미수신'}.{note}")
            else:
                self.log(f"{packet['batchId'][:8]}: 서버 접수 완료 (영수증 미조회). "
                         f"대기열 갱신 {count}건. 이 서버는 요청을 즉시 실행하지만 "
                         f"결과 확인은 '서버 영수증 자동 조회'로 다시 시도하세요.")
                if not silent:
                    QMessageBox.information(
                        self, "전송 완료",
                        "서버에 제출했습니다. (이 서버는 등록·삭제를 즉시 실행합니다.)\n"
                        "다만 영수증을 받지 못해 반영 결과는 확인하지 못했습니다.\n"
                        "'서버 영수증 자동 조회'를 눌러 확인하세요.")
            self.refresh_table()
        except MenloError as error:
            message = str(error)
            if "자격 증명" in message or "올바르지 않습니다" in message:
                self.log("전송 실패: 로그인 자격증명 오류. 비밀번호를 다시 입력하세요.")
                if not silent:
                    QMessageBox.warning(self, "로그인 실패", "사용자 이름 또는 비밀번호가 올바르지 않습니다.")
            elif packet and message.startswith("전송 거부:"):
                # 서버가 내용 검증에서 배치를 거부한 경우: 같은 내용을 다시 보내도
                # 결과가 같으므로 재시도를 막지 않도록 종료 상태로 닫는다.
                count = self.engine.reject_batch(packet["batchId"], message)
                self.log(f"{packet['batchId'][:8]}: {message} "
                         f"→ 거부된 요청 {count}건을 종료 처리했습니다. "
                         f"원본 데이터를 수정하면 다음 스캔에서 다시 등록됩니다.")
                self.refresh_table()
                if not silent:
                    QMessageBox.warning(
                        self, "서버가 요청을 거부했습니다",
                        f"{message}\n\n거부된 요청 {count}건을 종료 처리했습니다.\n"
                        f"원본 JSON을 수정하면 다음 스캔에서 다시 등록됩니다.")
            else:
                self.log(f"전송 실패: {message}")
                if not silent:
                    QMessageBox.warning(self, "전송 실패", message)
        except ValueError as error:
            if not silent:
                QMessageBox.warning(self, "설정 오류", str(error))

    def fetch_receipts(self, silent: bool = False):
        try:
            origin = self.origin_url()
            username = self.server_username.text().strip()
            password = self.ensure_password()
            if not password:
                return
            transport = self.ensure_transport()
            try:
                transport.ensure_logged_in(username, password)
                doc = transport.receipts()
            except MenloError:
                self.teardown_transport()
                raise
            result = self.engine.load_receipt(json.dumps(doc, ensure_ascii=False))
            self.log(f"서버 영수증 갱신 {result['updated']}건")
            if result.get("requeued"):
                ids = ", ".join(batch[:8] for batch in result["requeued"])
                self.log(f"서버에 없던 배치 {len(result['requeued'])}개({ids})를 "
                         f"다시 전송 대기로 되돌렸습니다.")
            for error in result["errors"]:
                self.log("검증 실패: " + error)
            self.refresh_table()
        except MenloError as error:
            self.log(f"영수증 조회 실패: {error}")
            if not silent:
                QMessageBox.warning(self, "영수증 조회 실패", str(error))
        except ValueError as error:
            if not silent:
                QMessageBox.warning(self, "설정 오류", str(error))

    def refresh_table(self):
        rows = self.engine.queue_rows()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate((
                row["id"], row["action"], row["status"], row["ref"],
                row["batch_id"] or ""
            )):
                item = QTableWidgetItem(str(value))
                if j == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()

    def prepare(self):
        packet = self.engine.prepare()
        if not packet:
            QMessageBox.information(self, "전송할 항목 없음", "준비할 새 요청이 없습니다.")
            return
        self.last_batch = packet["batchId"]
        raw = json.dumps(packet, ensure_ascii=False, indent=2)
        QApplication.clipboard().setText(raw)
        self.refresh_table()
        dialog = QDialog(self)
        dialog.setWindowTitle("Menlo 웹페이지에 붙여넣을 JSON")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("클립보드에 복사했습니다. CalSync 웹페이지에 붙여넣어 제출하세요.\n"
                                "만약 Menlo가 복사를 차단한다면 기관 정책을 확인하세요."))
        editor = QTextEdit()
        editor.setPlainText(raw)
        editor.setReadOnly(True)
        layout.addWidget(editor)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
        self.log(f"{self.last_batch}: JSON 전송 준비됨 (아직 실제 서버로 보내지 않음)")

    def mark_submitted(self):
        if not self.last_batch:
            QMessageBox.information(self, "Batch 미선택", "전송 데이터 준비를 먼저 수행하세요.")
            return
        answer = QMessageBox.question(
            self, "웹 제출 표시",
            f"batch {self.last_batch[:8]}을(를) 실제로 CalSync 웹에서 제출했나요?\n\n"
            "'예'를 누르면 서버 수신 확인 없이 전송된 것으로 표시합니다.\n"
            "실제로 제출하지 않았다면 이 일정들은 서버에 영원히 전달되지 않습니다.\n"
            "가능하면 '서버로 직접 전송'을 사용하세요.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.log("웹 제출 표시를 취소했습니다. '서버로 직접 전송' 사용을 권장합니다.")
            return
        count = self.engine.mark_submitted(self.last_batch)
        self.log(f"웹 제출했다고 표시한 요청 {count}건. 서버 수신 영수증 확인 전입니다. "
                 f"'서버 영수증 자동 조회'로 접수를 확인하세요.")
        self.refresh_table()

    def import_receipt(self):
        raw, ok = QInputDialog.getMultiLineText(
            self, "서버 수신 상태 가져오기",
            "Menlo 웹페이지에서 복사한 JSON 영수증/상태를 붙여넣으세요.")
        if not ok or not raw.strip():
            return
        try:
            result = self.engine.load_receipt(raw)
            self.log(f"서버 응답 갱신 {result['updated']}건")
            for error in result["errors"]:
                self.log("검증 실패: " + error)
            self.refresh_table()
        except (ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, "영수증 오류", str(error))

    def open_site(self):
        parsed = urlsplit(self.url.text().strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            QMessageBox.warning(self, "HTTPS 주소 오류", "https://도메인 형식만 허용됩니다.")
            return
        self.save_settings()
        QDesktopServices.openUrl(QUrl(self.url.text().strip()))
        self.log("기본 브라우저를 열었습니다. Menlo 경유인지 직접 확인하세요.")

    def restore(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def sync_now(self):
        """트레이 '지금동기화': 새 변경을 스캔한 뒤 서버로 전송하고 트레이 알림을 띄운다."""
        self.scan()
        self.send_direct(silent=True)
        last = self.logs.toPlainText().strip().splitlines()
        msg = last[-1].split("] ", 1)[-1] if last else "동기화 완료"
        self.tray.showMessage("CalSync 동기화", msg,
                              QSystemTrayIcon.MessageIcon.Information, 3000)

    def show_log(self):
        """트레이 '로그보기': 이번 달 로그 파일(YYYYMM.log)을 새 창에 연다."""
        from datetime import datetime
        current = datetime.now().strftime("%Y%m")
        path = data_folder() / f"{current}.log"
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            text = f"{current}.log 파일이 아직 없습니다.\n전송/감시를 실행하면 로그가 쌓입니다."
        dialog = QDialog(self)
        dialog.setWindowTitle(f"CalSync 로그 - {current}.log")
        dialog.resize(760, 460)
        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(editor)
        dialog.exec()

    def quit(self):
        self.exiting = True
        self.timer.stop()
        self.debounce.stop()
        self.save_settings()
        self.tray.hide()
        self.teardown_transport()
        self.engine.close()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent):
        if self.exiting:
            event.accept()
            return
        if self.monitoring and QSystemTrayIcon.isSystemTrayAvailable():
            answer = QMessageBox(self)
            answer.setWindowTitle("CalSync 자동 감시 실행 중")
            answer.setText("JSON 자동 감시가 실행 중입니다. 어떻게 하시겠습니까?")
            to_tray = answer.addButton("트레이로", QMessageBox.ButtonRole.AcceptRole)
            leave = answer.addButton("완전히 종료", QMessageBox.ButtonRole.DestructiveRole)
            cancel = answer.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            answer.setDefaultButton(to_tray)
            answer.exec()
            chosen = answer.clickedButton()
            if chosen == to_tray:
                event.ignore()
                self.hide()
                self.tray.showMessage("CalSync", "트레이에서 JSON 감시를 계속합니다.",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)
            elif chosen == leave:
                self.quit()
                event.accept()
            else:
                event.ignore()
        else:
            self.quit()
            event.accept()


def self_test(origin: str) -> int:
    """Edge·Playwright·Menlo 경로와 자격 증명 백엔드가 이 실행 파일에서 동작하는지 확인한다."""
    log_path = data_folder() / "self-test.log"
    try:
        with MenloTransport(origin, timeout_ms=60_000) as transport:
            transport.page.goto(origin + "/login", wait_until="load", timeout=60_000)
            transport.page.get_by_role("textbox", name="사용자 이름").wait_for(
                state="visible", timeout=45_000
            )
        backend = type(__import__("keyring").get_keyring()).__name__
        log_password = "yes" if load_password("__self-test__") else "no"
        log_path.write_text(f"ok keyring={backend} stored={log_password}", encoding="utf-8")
        return 0
    except Exception as error:  # noqa: BLE001 - 결과를 파일로 남겨야 한다
        log_path.write_text(f"fail {type(error).__name__}: {error}", encoding="utf-8")
        return 1


def main():
    if "--self-test" in sys.argv:
        position = sys.argv.index("--self-test")
        origin = sys.argv[position + 1] if len(sys.argv) > position + 1 else os.environ.get(
            "CALSYNC_ORIGIN", "https://calendar.example.com")
        raise SystemExit(self_test(origin))
    app = QApplication(sys.argv)
    app.setApplicationName("CalSync Windows")
    app.setQuitOnLastWindowClosed(False)
    client = Client()
    # Auto-start only through explicit HKCU Run registration.
    if "--tray" in sys.argv and QSystemTrayIcon.isSystemTrayAvailable():
        client.hide()
    else:
        client.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
