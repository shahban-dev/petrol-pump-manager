"""Owner login dialog: first-run setup, login, and password reset.

Flows:
- First launch (no users): setup form — username, password + confirm,
  two security questions + answers. Creates the owner account.
- Later launches: username + password, with a "Forgot password?" link.
- Reset: username -> security questions -> answers -> new password.

Security notes:
- Password fields always use QLineEdit.Password echo mode.
- Error messages never reveal whether a username exists; the service
  raises the same "Invalid username or password." either way, and the
  dialog reuses it for the question-lookup path too.
- No passwords or answers are logged or displayed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.localization import tr
from app.services.auth_service import AuthError, AuthService, UserInfo
from app.services import errors as service_errors


class _Page(QWidget):
    """Base page: form + inline error label."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 20, 24, 20)
        self._layout.setSpacing(10)
        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 17px; font-weight: bold;")
        self._layout.addWidget(self._title)
        self._form = QFormLayout()
        self._form.setSpacing(8)
        self._layout.addLayout(self._form)
        self._error = QLabel(self)
        self._error.setProperty("alert", True)
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        self._layout.addWidget(self._error)
        self._layout.addStretch(1)
        self._buttons = QHBoxLayout()
        self._buttons.addStretch(1)
        self._layout.addLayout(self._buttons)

    def set_error(self, text: str | None) -> None:
        self._error.setVisible(bool(text))
        self._error.setText(text or "")

    def add_button(self, button: QPushButton) -> None:
        self._buttons.addWidget(button)


class _SetupPage(_Page):
    """First-run owner account creation."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.username_edit = QLineEdit(self)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit = QLineEdit(self)
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        self.q1_edit = QLineEdit(self)
        self.a1_edit = QLineEdit(self)
        self.a1_edit.setEchoMode(QLineEdit.Password)
        self.q2_edit = QLineEdit(self)
        self.a2_edit = QLineEdit(self)
        self.a2_edit.setEchoMode(QLineEdit.Password)
        self._form.addRow(self._mk_label("auth.username"), self.username_edit)
        self._form.addRow(self._mk_label("auth.password"), self.password_edit)
        self._form.addRow(
            self._mk_label("auth.confirm_password"), self.confirm_edit
        )
        self._form.addRow(self._mk_label("auth.question_1"), self.q1_edit)
        self._form.addRow(self._mk_label("auth.answer_1"), self.a1_edit)
        self._form.addRow(self._mk_label("auth.question_2"), self.q2_edit)
        self._form.addRow(self._mk_label("auth.answer_2"), self.a2_edit)
        self.create_btn = QPushButton(self)
        self.create_btn.setProperty("btn", "primary")
        self.create_btn.setDefault(True)
        self.add_button(self.create_btn)
        self.retranslate()

    @staticmethod
    def _mk_label(key: str) -> QLabel:
        label = QLabel()
        label.setText(tr(key) + ":")
        return label

    def retranslate(self) -> None:
        self._title.setText(tr("auth.setup_title"))
        self.create_btn.setText(tr("auth.create_account"))


class _LoginPage(_Page):
    """Username + password with a forgot-password link."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.username_edit = QLineEdit(self)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.Password)
        self._form.addRow(self._mk_label("auth.username"), self.username_edit)
        self._form.addRow(self._mk_label("auth.password"), self.password_edit)
        self._forgot = QLabel(self)
        self._forgot.setTextFormat(Qt.RichText)
        self._forgot.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self._forgot.linkActivated.connect(self._on_forgot_clicked)
        self._forgot_handler = None
        link_row = QHBoxLayout()
        link_row.addStretch(1)
        link_row.addWidget(self._forgot)
        self._layout.insertLayout(3, link_row)
        self.login_btn = QPushButton(self)
        self.login_btn.setProperty("btn", "primary")
        self.login_btn.setDefault(True)
        self.add_button(self.login_btn)
        self.retranslate()

    @staticmethod
    def _mk_label(key: str) -> QLabel:
        label = QLabel()
        label.setText(tr(key) + ":")
        return label

    def _on_forgot_clicked(self, _link: str) -> None:
        if self._forgot_handler is not None:
            self._forgot_handler()

    def on_forgot(self, handler) -> None:
        self._forgot_handler = handler

    def retranslate(self) -> None:
        self._title.setText(tr("auth.login_title"))
        self._forgot.setText(
            f'<a href="forgot">{tr("auth.forgot_password")}</a>'
        )
        self.login_btn.setText(tr("auth.login"))


class _ResetNamePage(_Page):
    """Step 1 of reset: which account."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.username_edit = QLineEdit(self)
        self._form.addRow(self._mk_label("auth.username"), self.username_edit)
        self.next_btn = QPushButton(self)
        self.next_btn.setProperty("btn", "primary")
        self.next_btn.setDefault(True)
        self.back_btn = QPushButton(self)
        self.add_button(self.back_btn)
        self.add_button(self.next_btn)
        self.retranslate()

    @staticmethod
    def _mk_label(key: str) -> QLabel:
        label = QLabel()
        label.setText(tr(key) + ":")
        return label

    def retranslate(self) -> None:
        self._title.setText(tr("auth.reset_title"))
        self.next_btn.setText(tr("auth.next"))
        self.back_btn.setText(tr("auth.back"))


class _ResetQuestionsPage(_Page):
    """Step 2 of reset: answer the two security questions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.q1_label = QLabel(self)
        self.q1_label.setWordWrap(True)
        self.q1_label.setStyleSheet("font-weight: bold;")
        self.a1_edit = QLineEdit(self)
        self.a1_edit.setEchoMode(QLineEdit.Password)
        self.q2_label = QLabel(self)
        self.q2_label.setWordWrap(True)
        self.q2_label.setStyleSheet("font-weight: bold;")
        self.a2_edit = QLineEdit(self)
        self.a2_edit.setEchoMode(QLineEdit.Password)
        self._form.addRow(self.q1_label, self.a1_edit)
        self._form.addRow(self.q2_label, self.a2_edit)
        self.next_btn = QPushButton(self)
        self.next_btn.setProperty("btn", "primary")
        self.next_btn.setDefault(True)
        self.back_btn = QPushButton(self)
        self.add_button(self.back_btn)
        self.add_button(self.next_btn)
        self.retranslate()

    def retranslate(self) -> None:
        self._title.setText(tr("auth.reset_title"))
        self.next_btn.setText(tr("auth.next"))
        self.back_btn.setText(tr("auth.back"))


class _ResetPasswordPage(_Page):
    """Step 3 of reset: the new password."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit = QLineEdit(self)
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        self._form.addRow(self._mk_label("auth.new_password"), self.password_edit)
        self._form.addRow(
            self._mk_label("auth.confirm_password"), self.confirm_edit
        )
        self.reset_btn = QPushButton(self)
        self.reset_btn.setProperty("btn", "primary")
        self.reset_btn.setDefault(True)
        self.back_btn = QPushButton(self)
        self.add_button(self.back_btn)
        self.add_button(self.reset_btn)
        self.retranslate()

    @staticmethod
    def _mk_label(key: str) -> QLabel:
        label = QLabel()
        label.setText(tr(key) + ":")
        return label

    def retranslate(self) -> None:
        self._title.setText(tr("auth.reset_title"))
        self.reset_btn.setText(tr("auth.reset_password"))
        self.back_btn.setText(tr("auth.back"))


class LoginDialog(QDialog):
    """Modal gate shown before the main window.

    On success, ``accept()`` is called and ``.username`` holds the
    authenticated owner's username. On close/cancel the dialog is
    rejected and the app exits cleanly.
    """

    def __init__(self, auth: AuthService, parent=None) -> None:
        super().__init__(parent)
        self._auth = auth
        self.username: str | None = None
        self._reset_username = ""
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setWindowTitle(tr("auth.window_title"))

        self._stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self._stack)

        self._setup_page = _SetupPage(self)
        self._login_page = _LoginPage(self)
        self._reset_name_page = _ResetNamePage(self)
        self._reset_questions_page = _ResetQuestionsPage(self)
        self._reset_password_page = _ResetPasswordPage(self)
        for page in (
            self._setup_page,
            self._login_page,
            self._reset_name_page,
            self._reset_questions_page,
            self._reset_password_page,
        ):
            self._stack.addWidget(page)

        self._setup_page.create_btn.clicked.connect(self._do_setup)
        self._login_page.login_btn.clicked.connect(self._do_login)
        self._login_page.on_forgot(lambda: self._go(self._reset_name_page))
        self._reset_name_page.next_btn.clicked.connect(self._do_reset_lookup)
        self._reset_name_page.back_btn.clicked.connect(
            lambda: self._go(self._login_page)
        )
        self._reset_questions_page.next_btn.clicked.connect(
            lambda: self._go(self._reset_password_page)
        )
        self._reset_questions_page.back_btn.clicked.connect(
            lambda: self._go(self._reset_name_page)
        )
        self._reset_password_page.reset_btn.clicked.connect(self._do_reset)
        self._reset_password_page.back_btn.clicked.connect(
            lambda: self._go(self._reset_questions_page)
        )
        # Enter key submits whichever page is visible.
        self._login_page.password_edit.returnPressed.connect(self._do_login)
        self._setup_page.confirm_edit.returnPressed.connect(self._do_setup)

        if self._auth.needs_setup():
            self._go(self._setup_page)
        else:
            self._go(self._login_page)

    # ------------------------------------------------------------------
    def _go(self, page: QWidget) -> None:
        page.set_error(None)
        self._stack.setCurrentWidget(page)

    # ------------------------------------------------------------------
    def _do_setup(self) -> None:
        page = self._setup_page
        page.set_error(None)
        username = page.username_edit.text().strip()
        password = page.password_edit.text()
        confirm = page.confirm_edit.text()
        if password != confirm:
            page.set_error(tr("auth.passwords_mismatch"))
            return
        try:
            info = self._auth.setup_owner(
                username,
                password,
                page.q1_edit.text(),
                page.a1_edit.text(),
                page.q2_edit.text(),
                page.a2_edit.text(),
            )
        except (service_errors.ValidationError, AuthError) as exc:
            page.set_error(self._friendly(exc))
            return
        except Exception:  # noqa: BLE001 — never leak internals
            page.set_error(tr("common.unexpected_error"))
            return
        self._finish(info)

    def _do_login(self) -> None:
        page = self._login_page
        page.set_error(None)
        try:
            info = self._auth.authenticate(
                page.username_edit.text(), page.password_edit.text()
            )
        except AuthError as exc:
            # Lockout carries its own message; bad credentials stay vague.
            page.set_error(self._friendly(exc))
            return
        except Exception:  # noqa: BLE001
            page.set_error(tr("common.unexpected_error"))
            return
        page.password_edit.clear()
        self._finish(info)

    # ------------------------------------------------------------------
    def _do_reset_lookup(self) -> None:
        page = self._reset_name_page
        page.set_error(None)
        username = page.username_edit.text().strip()
        self._reset_username = username
        try:
            q1, q2 = self._auth.security_questions(username)
        except AuthError as exc:
            # Never reveal whether the username exists.
            page.set_error(self._friendly(exc))
            return
        except Exception:  # noqa: BLE001
            page.set_error(tr("common.unexpected_error"))
            return
        self._reset_questions_page.q1_label.setText(q1)
        self._reset_questions_page.q2_label.setText(q2)
        self._reset_questions_page.a1_edit.clear()
        self._reset_questions_page.a2_edit.clear()
        self._go(self._reset_questions_page)

    def _do_reset(self) -> None:
        page = self._reset_password_page
        page.set_error(None)
        password = page.password_edit.text()
        confirm = page.confirm_edit.text()
        if password != confirm:
            page.set_error(tr("auth.passwords_mismatch"))
            return
        try:
            self._auth.reset_password(
                self._reset_username,
                self._reset_questions_page.a1_edit.text(),
                self._reset_questions_page.a2_edit.text(),
                password,
            )
        except (AuthError, service_errors.ValidationError) as exc:
            page.set_error(self._friendly(exc))
            return
        except Exception:  # noqa: BLE001
            page.set_error(tr("common.unexpected_error"))
            return
        # Reset succeeded: return to login so the owner signs in fresh.
        page.password_edit.clear()
        page.confirm_edit.clear()
        self._reset_questions_page.a1_edit.clear()
        self._reset_questions_page.a2_edit.clear()
        self._login_page.username_edit.setText(self._reset_username)
        self._go(self._login_page)

    # ------------------------------------------------------------------
    def _finish(self, info: UserInfo) -> None:
        self.username = info.username
        self.accept()

    @staticmethod
    def _friendly(exc: Exception) -> str:
        """Service error -> display text, never a traceback."""
        from app.localization import localize_error

        if isinstance(exc, service_errors.BusinessRuleError):
            return localize_error(exc)
        return str(exc)
