"""테스트 전용 헬퍼 (sandbox_worker subprocess 목 등)."""

import threading
from unittest.mock import MagicMock

import sandbox_worker


class FakeReadableStream:
    """Popen stdout/stderr 텍스트 스트림을 흉내낸다. EOF 시 한 번만 콜백을 호출한다."""

    def __init__(self, text: str | None, on_eof_once):
        self._lines = (text or '').splitlines(keepends=True)
        self._i = 0
        self._on_eof_once = on_eof_once

    def readline(self):
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        if self._on_eof_once is not None:
            cb = self._on_eof_once
            self._on_eof_once = None
            cb()
        return ''

    def close(self):
        pass


def install_claude_popen_mock(
    monkeypatch,
    *,
    stdout_text: str,
    returncode: int = 0,
    stderr_text: str = '',
    cmd_capture: dict | None = None,
):
    """`sandbox_worker.subprocess.Popen`을 패치해 실제 claude 없이 stdout/stderr를 시뮬레이션한다."""

    done = threading.Event()
    pending = [2]

    def mark_done():
        pending[0] -= 1
        if pending[0] == 0:
            done.set()

    stdout_stream = FakeReadableStream(stdout_text, mark_done)
    stderr_stream = FakeReadableStream(stderr_text, mark_done)

    class FakeProc:
        def __init__(self):
            self.stdout = stdout_stream
            self.stderr = stderr_stream
            self.returncode = returncode
            self.kill = MagicMock()

        def poll(self):
            return self.returncode if done.is_set() else None

    def factory(cmd, *_args, **_kwargs):
        if cmd_capture is not None:
            cmd_capture['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(sandbox_worker.subprocess, 'Popen', factory)
