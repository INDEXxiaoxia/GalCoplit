# -*- coding: utf-8 -*-
"""PaddleOCR 旁路进程：本地 HTTP 服务，供主程序按需调用。

固定使用 PP-OCRv6_small（需 paddleocr>=3.7）。

用法（在 paddle_ocr_cpu / paddle_ocr_gpu 插件目录的解释器里运行）:
    python paddle_ocr_worker.py --device cpu
    python paddle_ocr_worker.py --device gpu --port 0

就绪后向 stdout 打印一行:
    GALCOPLIT_OCR_READY port=<端口>
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# 固定模型：桌面/Gal 对话框场景速度与精度平衡档
DET_MODEL_NAME = "PP-OCRv6_small_det"
REC_MODEL_NAME = "PP-OCRv6_small_rec"
MIN_PADDLEOCR_VERSION = (3, 7, 0)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _parse_version(text: str) -> tuple:
    parts = []
    for p in (text or "0").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _to_paddle_lang(lang: str) -> str:
    """将配置里的语言码转为 Paddle 侧常用值（v6_small 为统一多语模型，lang 仅作记录）。"""
    raw = (lang or "chi_sim").strip().lower()
    mapping = {
        "chi_sim": "ch",
        "chi_tra": "chinese_cht",
        "chi": "ch",
        "zh": "ch",
        "ch": "ch",
        "chinese_cht": "chinese_cht",
        "jpn": "japan",
        "ja": "japan",
        "japan": "japan",
        "eng": "en",
        "en": "en",
        "fra": "fr",
        "fr": "fr",
        "french": "fr",
        "deu": "de",
        "de": "de",
        "german": "de",
        "spa": "es",
        "es": "es",
        "kor": "korean",
        "ko": "korean",
        "korean": "korean",
    }
    if raw in mapping:
        return mapping[raw]
    head = raw.split("_")[0]
    return mapping.get(head, "ch")


class PaddleEngine:
    """懒加载固定 PP-OCRv6_small；按 det_limit 缓存实例。"""

    def __init__(self, device: str):
        self.device = device if device in ("cpu", "gpu") else "cpu"
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._import_error = ""
        self._paddle_ready = False
        self._cuda = False
        self._paddleocr_version = ""
        self._prepare_env()

    def _prepare_env(self) -> None:
        os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
        os.environ["FLAGS_use_mkldnn"] = "0"
        if self.device == "cpu":
            # 避免本机装了 CUDA 时 paddle 误抢 GPU
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
        try:
            import paddle  # noqa: F401
            import paddleocr
            from paddleocr import PaddleOCR  # noqa: F401

            self._paddleocr_version = getattr(paddleocr, "__version__", "") or ""
            ver = _parse_version(self._paddleocr_version)
            if ver < MIN_PADDLEOCR_VERSION:
                self._import_error = (
                    f"当前 paddleocr={self._paddleocr_version or '?'}，"
                    f"PP-OCRv6_small 需要 >= {'.'.join(map(str, MIN_PADDLEOCR_VERSION))}。"
                    "请删除插件目录后重新「安装环境」。"
                )
                self._paddle_ready = False
                _log(f"[paddle-worker] {self._import_error}")
                return

            self._paddle_ready = True
            try:
                import paddle as _paddle

                self._cuda = bool(_paddle.device.is_compiled_with_cuda())
            except Exception:
                self._cuda = False
        except Exception as e:
            self._import_error = str(e)
            self._paddle_ready = False
            _log(f"[paddle-worker] 导入失败: {e}")

    def info(self) -> dict:
        return {
            "ok": self._paddle_ready,
            "device": self.device,
            "cuda_compiled": self._cuda,
            "error": self._import_error,
            "paddleocr_version": self._paddleocr_version,
            "det_model": DET_MODEL_NAME,
            "rec_model": REC_MODEL_NAME,
        }

    def _create(self, det_limit: int = 0):
        from paddleocr import PaddleOCR

        device = "gpu:0" if self.device == "gpu" else "cpu"
        kwargs = {
            "text_detection_model_name": DET_MODEL_NAME,
            "text_recognition_model_name": REC_MODEL_NAME,
            "device": device,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
        if det_limit and det_limit > 0:
            kwargs["text_det_limit_side_len"] = int(det_limit)

        engine = PaddleOCR(**kwargs)
        _log(
            f"[paddle-worker] PaddleOCR 初始化成功 "
            f"models=({DET_MODEL_NAME}, {REC_MODEL_NAME}) device={device} "
            f"det_limit={det_limit or 'default'}"
        )
        return engine

    @staticmethod
    def _extract_text(result) -> str:
        """兼容 PaddleOCR 3.x predict() 返回结构。"""
        if result is None:
            return ""

        # 3.x: list[OCRResult/dict]，常见字段 rec_texts
        if isinstance(result, list) and result and not isinstance(result[0], list):
            texts = []
            for item in result:
                if item is None:
                    continue
                if isinstance(item, dict):
                    rec = item.get("rec_texts") or item.get("texts") or []
                    texts.extend([str(t) for t in rec if t])
                    continue
                rec = getattr(item, "rec_texts", None) or getattr(item, "texts", None)
                if rec:
                    texts.extend([str(t) for t in rec if t])
                    continue
                data = getattr(item, "json", None)
                if isinstance(data, dict):
                    rec = data.get("res", {}).get("rec_texts") or data.get("rec_texts") or []
                    texts.extend([str(t) for t in rec if t])
            if texts:
                return "\n".join(texts).strip()

        # 旧结构兜底
        lines = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
        if not lines:
            return ""
        texts = []
        for line in lines:
            if line and len(line) >= 2:
                piece = line[1]
                if isinstance(piece, (list, tuple)) and piece:
                    texts.append(str(piece[0]))
                else:
                    texts.append(str(piece))
        return "\n".join(texts).strip()

    def ocr(self, image_bytes: bytes, lang: str, det_limit: int = 0) -> str:
        if not self._paddle_ready:
            raise RuntimeError(self._import_error or "未安装 paddleocr / paddlepaddle")
        from PIL import Image
        import numpy as np

        paddle_lang = _to_paddle_lang(lang)
        cache_key = f"v6small:{int(det_limit) if det_limit and det_limit > 0 else 0}"
        with self._lock:
            if cache_key not in self._cache:
                _log(
                    f"[paddle-worker] 初始化模型 {DET_MODEL_NAME}/{REC_MODEL_NAME} "
                    f"device={self.device} lang_hint={paddle_lang} det_limit={det_limit or 'default'}"
                )
                self._cache[cache_key] = self._create(det_limit=det_limit)
            engine = self._cache[cache_key]

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
        predict_kwargs = {}
        if det_limit and det_limit > 0:
            predict_kwargs["text_det_limit_side_len"] = int(det_limit)

        if hasattr(engine, "predict"):
            try:
                result = engine.predict(arr, **predict_kwargs) if predict_kwargs else engine.predict(arr)
                return self._extract_text(result)
            except TypeError:
                # 个别版本 predict 不接受额外 kwargs
                result = engine.predict(arr)
                return self._extract_text(result)
            except Exception as e:
                _log(f"[paddle-worker] predict 失败，回退 ocr(): {e}")
        if hasattr(engine, "ocr"):
            result = engine.ocr(arr)
            return self._extract_text(result)
        raise RuntimeError("当前 PaddleOCR 无 predict/ocr 接口")


def make_handler(engine: PaddleEngine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            _log("[paddle-worker] " + (fmt % args))

        def _send_json(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._send_json(200, engine.info())
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/ocr":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                b64 = data.get("image_b64") or ""
                lang = data.get("lang") or "chi_sim"
                det_limit = int(data.get("det_limit") or 0)
                if not b64:
                    self._send_json(400, {"ok": False, "error": "缺少 image_b64"})
                    return
                image_bytes = base64.b64decode(b64)
                text = engine.ocr(image_bytes, lang, det_limit=det_limit)
                self._send_json(200, {"ok": True, "text": text})
            except Exception as e:
                _log(traceback.format_exc())
                self._send_json(500, {"ok": False, "error": str(e)})

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="GalCoplit PaddleOCR sidecar worker")
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--port", type=int, default=0, help="0 表示自动分配端口")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    engine = PaddleEngine(args.device)
    handler = make_handler(engine)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    port = server.server_address[1]
    # 主程序靠这一行发现端口；必须打到 stdout
    print(f"GALCOPLIT_OCR_READY port={port}", flush=True)
    _log(
        f"[paddle-worker] listening on http://{args.host}:{port} "
        f"device={args.device} model={DET_MODEL_NAME}/{REC_MODEL_NAME}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
