"""AstrBot plugin for transcribing uploaded audio into MIDI with Basic Pitch."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import shutil
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp


SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
DEFAULT_MAX_FILE_SIZE_MB = 50


class MidiTransfer(Star):
    """Receive audio messages and return a MIDI transcription."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._processed_message_ids: set[str] = set()
        self._processed_message_order: deque[str] = deque(maxlen=256)
        self._processed_messages_lock = asyncio.Lock()

    @property
    def max_file_size(self) -> int:
        configured = self.config.get("max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB)
        try:
            return max(1, int(configured)) * 1024 * 1024
        except (TypeError, ValueError):
            return DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def suppress_inference_logs(self) -> bool:
        return self.config.get("suppress_inference_logs", True) is not False

    @filter.command("midi")
    async def midi_command(self, event: AstrMessageEvent):
        """将本条消息中的音频转换为 MIDI。"""
        audio = await self._find_audio(event.get_messages())
        if audio is None:
            yield event.plain_result(
                "请引用或附加 mp3、wav、flac、m4a、ogg 音频后再发送 /midi。"
            )
            return
        async for result in self._convert_and_reply(event, audio):
            yield result

    async def _convert_and_reply(self, event: AstrMessageEvent, audio: dict[str, str]):
        message_id = str(getattr(event.message_obj, "message_id", ""))
        if message_id and not await self._claim_message(message_id):
            logger.debug(f"Skipping duplicate MIDI conversion for message {message_id}")
            return

        source_name = audio.get("name", "audio")
        suffix = Path(source_name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            yield event.plain_result("暂不支持该音频格式，仅支持 mp3、wav、flac、m4a、ogg。")
            return

        yield event.plain_result("正在转换音频，请稍候……")
        try:
            with tempfile.TemporaryDirectory(
                prefix="midi_transfer_", dir=self.data_dir
            ) as work_dir:
                input_path = Path(work_dir) / f"input{suffix}"
                await self._save_audio(audio, input_path)
                if input_path.stat().st_size == 0:
                    raise ValueError("音频文件为空")
                if input_path.stat().st_size > self.max_file_size:
                    limit = self.max_file_size // (1024 * 1024)
                    raise ValueError(f"文件超过 {limit} MB 限制")

                output_dir = Path(work_dir) / "output"
                output_dir.mkdir()
                await asyncio.to_thread(self._run_basic_pitch, input_path, output_dir)
                midi_path = next(output_dir.glob("*.mid"), None)
                if midi_path is None:
                    raise ValueError("未识别到可转换的音符")
                # Persist the result before yielding because adapters may send later,
                # after the temporary working directory has been cleaned up.
                result_path = self._build_result_path(source_name)
                await asyncio.to_thread(shutil.copyfile, midi_path, result_path)
                yield event.chain_result(
                    [
                        Comp.Plain("转换完成："),
                        Comp.File(name=result_path.name, file=str(result_path)),
                    ]
                )
        except ValueError as exc:
            yield event.plain_result(f"转换失败：{exc}")
        except Exception as exc:
            logger.exception("Basic Pitch transcription failed")
            yield event.plain_result(f"转换失败：{self._friendly_error(exc)}")

    def _build_result_path(self, source_name: str) -> Path:
        stem = Path(source_name).stem or "audio"
        safe_stem = "".join(
            char if char not in '<>:\"/\\|?*' and ord(char) >= 32 else "_"
            for char in stem
        ).strip(". ")
        safe_stem = safe_stem[:100] or "audio"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_path = self.data_dir / f"{safe_stem}_{timestamp}.mid"
        if not base_path.exists():
            return base_path
        return self.data_dir / f"{safe_stem}_{timestamp}_{uuid.uuid4().hex[:8]}.mid"

    async def _claim_message(self, message_id: str) -> bool:
        async with self._processed_messages_lock:
            if message_id in self._processed_message_ids:
                return False
            if len(self._processed_message_order) == self._processed_message_order.maxlen:
                expired = self._processed_message_order.popleft()
                self._processed_message_ids.discard(expired)
            self._processed_message_order.append(message_id)
            self._processed_message_ids.add(message_id)
            return True

    async def _save_audio(self, audio: dict[str, str], destination: Path) -> None:
        local_path = audio.get("path")
        if local_path:
            await asyncio.to_thread(shutil.copyfile, local_path, destination)
            return
        url = audio.get("url")
        if not url:
            raise ValueError("无法取得音频下载地址")
        timeout = httpx.Timeout(120.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                size = 0
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_file_size:
                            raise ValueError(
                                f"文件超过 {self.max_file_size // (1024 * 1024)} MB 限制"
                            )
                        output.write(chunk)

    def _run_basic_pitch(self, input_path: Path, output_dir: Path) -> None:
        with self._inference_log_context():
            try:
                from basic_pitch.inference import predict_and_save
            except ImportError as exc:
                raise ValueError(
                    "未安装 basic-pitch，请先安装 requirements.txt 中的依赖"
                ) from exc

            model = self._get_model()
            kwargs: dict[str, Any] = {
                "save_midi": True,
                "sonify_midi": False,
                "save_model_outputs": False,
                "save_notes": False,
            }
            if model is not None:
                kwargs["model_or_model_path"] = model
            predict_and_save([str(input_path)], str(output_dir), **kwargs)

    @contextlib.contextmanager
    def _inference_log_context(self):
        if not self.suppress_inference_logs:
            yield
            return

        # Set before TensorFlow import and temporarily silence third-party
        # logging, including Numba's first-run compilation diagnostics.
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        previous_levels = {
            name: logging.getLogger(name).level
            for name in ("tensorflow", "numba", "h5py", "absl")
        }
        for name in previous_levels:
            logging.getLogger(name).setLevel(logging.ERROR)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                yield
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                try:
                    from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
                    from basic_pitch.inference import Model

                    # Basic Pitch 0.4.0 defaults to its legacy TensorFlow SavedModel
                    # whenever TensorFlow is installed. That artifact cannot be loaded
                    # by TensorFlow 2.15 on Windows (Pad_2:output:0), while the bundled
                    # TFLite model works through tensorflow.lite.
                    bundled_model_path = build_icassp_2022_model_path(
                        FilenameSuffix.tflite
                    )
                    # TensorFlow Lite on Windows cannot open this model when the
                    # Python environment path contains non-ASCII characters.
                    # Copy it to the system temp directory, whose path is ASCII in
                    # normal Windows installations, and keep the cached model there.
                    model_path = (
                        Path(tempfile.gettempdir())
                        / "astrbot_basic_pitch_nmp_0_4_0.tflite"
                    )
                    if (
                        not model_path.exists()
                        or model_path.stat().st_size
                        != bundled_model_path.stat().st_size
                    ):
                        shutil.copyfile(bundled_model_path, model_path)
                    self._model = Model(model_path)
                except ImportError:
                    from basic_pitch import ICASSP_2022_MODEL_PATH

                    try:
                        from basic_pitch.inference import load_model

                        self._model = load_model(ICASSP_2022_MODEL_PATH)
                    except ImportError:
                        # Older Basic Pitch releases accept the model path directly.
                        self._model = ICASSP_2022_MODEL_PATH
        return self._model

    @classmethod
    async def _find_audio(cls, messages: Any) -> dict[str, str] | None:
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        for message in messages:
            # WebChat and several adapters preserve referenced content in a
            # Reply component's chain, so `/midi` can target quoted audio.
            quoted_chain = getattr(message, "chain", None)
            if quoted_chain:
                audio = await cls._find_audio(quoted_chain)
                if audio is not None:
                    return audio

            name = getattr(message, "name", None) or getattr(message, "file_name", None)
            local_path = getattr(message, "path", None) or getattr(message, "file_", None)
            url = getattr(message, "url", None)
            if isinstance(local_path, Path):
                local_path = str(local_path)
            candidate = str(name or local_path or urlparse(str(url)).path).lower()
            if Path(candidate).suffix not in SUPPORTED_EXTENSIONS:
                continue

            get_file = getattr(message, "get_file", None)
            if callable(get_file):
                local_path = await get_file()
            if not local_path and not url:
                continue
            return {
                "name": str(name or Path(candidate).name),
                "path": str(local_path or ""),
                "url": str(url or ""),
            }
        return None

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc).strip()
        if "No such file" in text or "audio" in text.lower() and "load" in text.lower():
            return "音频无法读取，请确认文件未损坏且格式正确"
        return text or "内部处理错误"

    async def terminate(self):
        """Release plugin resources when unloaded."""
        self._model = None
