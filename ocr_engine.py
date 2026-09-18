# -*- coding: utf-8 -*-
import io
import os
import numpy as np
from PIL import Image, ImageOps, ImageFilter

# Tesseract OCR
try:
    import pytesseract
    _PYTESSERACT = True
except ImportError:
    _PYTESSERACT = False

# Windows 10/11 自带 OCR（可选）
try:
    import asyncio
    from winrt.windows.media.ocr import OcrEngine  # type: ignore
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter  # type: ignore
    from winrt.windows.graphics.imaging import BitmapDecoder  # type: ignore
    _WINRT = True
except ImportError:
    _WINRT = False

# RapidOCR（主程序内置）
try:
    from rapidocr_onnxruntime import RapidOCR
    _RAPID = True
except ImportError:
    _RAPID = False

# Paddle 仅通过旁路插件调用，主进程不再 import paddleocr
from paddle_plugin import (
    BACKEND_CPU,
    BACKEND_GPU,
    backend_to_device,
    describe_backends,
    get_manager,
)

SUPPORTED_BACKENDS = ["tesseract", "windows", "rapid", BACKEND_CPU, BACKEND_GPU]

DEFAULT_OCR_LANG = "chi_sim"

# 各后端可用的语言码（写入 config 的值）。Rapid 为固定中文 ONNX；
# Paddle 使用 PP-OCRv6_small 统一多语模型（无韩语专用包，故不含 kor）。
OCR_LANG_OPTIONS = {
    "rapid": ["chi_sim"],
    "tesseract": [
        "chi_sim",
        "chi_tra",
        "eng",
        "jpn",
        "kor",
        "deu",
        "fra",
        "spa",
    ],
    "windows": ["chi_sim", "eng", "jpn"],
    BACKEND_CPU: [
        "chi_sim",
        "chi_tra",
        "eng",
        "jpn",
        "deu",
        "fra",
        "spa",
    ],
    BACKEND_GPU: [
        "chi_sim",
        "chi_tra",
        "eng",
        "jpn",
        "deu",
        "fra",
        "spa",
    ],
}

OCR_LANG_LABELS = {
    "chi_sim": "中文简体",
    "chi_tra": "中文繁体",
    "eng": "英语",
    "jpn": "日语",
    "kor": "韩语",
    "deu": "德语",
    "fra": "法语",
    "spa": "西班牙语",
}


def set_tesseract_cmd(path: str):
    if _PYTESSERACT and path and os.path.exists(path):
        pytesseract.pytesseract.tesseract_cmd = path
        tessdata = os.path.join(os.path.dirname(path), "tessdata")
        if os.path.isdir(tessdata):
            os.environ["TESSDATA_PREFIX"] = tessdata


def _preprocess(pil_image: Image.Image) -> Image.Image:
    """对截图进行预处理，提升 Tesseract 对游戏文字的识别率"""
    gray = pil_image.convert("L")
    w, h = gray.size
    enlarged = gray.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
    enhanced = ImageOps.autocontrast(enlarged)
    denoised = enhanced.filter(ImageFilter.MedianFilter(size=3))
    return denoised


def _tesseract_ocr(pil_image: Image.Image, lang: str) -> str:
    if not _PYTESSERACT:
        raise RuntimeError("未安装 pytesseract")
    img = _preprocess(pil_image)
    config = "--psm 6 -c preserve_interword_spaces=1"
    return pytesseract.image_to_string(img, lang=lang, config=config).strip()


async def _windows_ocr_async(pil_image: Image.Image) -> str:
    """使用 Windows 10/11 内置 OCR"""
    png_buffer = io.BytesIO()
    pil_image.save(png_buffer, format="PNG")
    png_bytes = png_buffer.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.default
    if engine is None:
        return ""
    result = await engine.recognize_async(bitmap)
    return result.text or ""


def _windows_ocr(pil_image: Image.Image) -> str:
    return asyncio.run(_windows_ocr_async(pil_image))


def _paddle_ocr(
    pil_image: Image.Image, lang: str, backend: str, det_limit: int = 0
) -> str:
    """通过旁路 worker 调用 PaddleOCR（cpu/gpu 插件二选一）"""
    device = backend_to_device(backend)
    return get_manager().ocr_image(
        pil_image, lang=lang, device=device, det_limit=det_limit
    )


# RapidOCR 实例缓存（按检测分辨率分键，改配置可重建）
_rapid_ocr_cache = {}


def _get_rapid_ocr(det_limit: int = 0) -> "RapidOCR":
    if not _RAPID:
        raise RuntimeError("未安装 rapidocr_onnxruntime")
    key = det_limit if det_limit and det_limit > 0 else "default"
    if key not in _rapid_ocr_cache:
        if det_limit and det_limit > 0:
            _rapid_ocr_cache[key] = RapidOCR(det_limit_side_len=det_limit)
        else:
            _rapid_ocr_cache[key] = RapidOCR()
    return _rapid_ocr_cache[key]


def _rapid_ocr_full(pil_image: Image.Image, det_limit: int = 0) -> tuple:
    """使用 RapidOCR 识别，返回 (text, avg_confidence)"""
    ocr = _get_rapid_ocr(det_limit)
    result, _ = ocr(np.array(pil_image))
    if not result:
        return "", 0.0
    texts = []
    scores = []
    for line in result:
        if line and len(line) >= 2:
            # rapidocr 返回结构：[box, text, score]
            texts.append(line[1])
            if len(line) >= 3 and isinstance(line[2], (int, float)):
                scores.append(float(line[2]))
    text = "\n".join(texts).strip()
    conf = (sum(scores) / len(scores)) if scores else 0.0
    return text, conf


def _rapid_ocr(pil_image: Image.Image, det_limit: int = 0) -> str:
    """使用 RapidOCR 识别（仅文本）"""
    text, _ = _rapid_ocr_full(pil_image, det_limit)
    return text


def normalize_backend(backend: str) -> str:
    """兼容旧配置 paddle → paddle-cpu。"""
    if backend == "paddle":
        return BACKEND_CPU
    return backend


def langs_for_backend(backend: str) -> list:
    """返回指定 OCR 后端可用的语言码列表，默认中文简体优先。"""
    backend = normalize_backend(backend or "rapid")
    langs = list(OCR_LANG_OPTIONS.get(backend, OCR_LANG_OPTIONS["rapid"]))
    if DEFAULT_OCR_LANG in langs:
        langs = [DEFAULT_OCR_LANG] + [x for x in langs if x != DEFAULT_OCR_LANG]
    return langs


def resolve_ocr_lang(backend: str, lang: str) -> str:
    """若当前语言不属于该后端，回退到默认中文。"""
    langs = langs_for_backend(backend)
    text = (lang or "").strip()
    if text in langs:
        return text
    return langs[0] if langs else DEFAULT_OCR_LANG


def paddle_plugin_status(app_dir=None) -> dict:
    return describe_backends(app_dir)


def ocr_image_extended(
    pil_image: Image.Image,
    lang: str = "chi_sim",
    backend: str = "tesseract",
    det_limit: int = 0,
) -> tuple:
    """按指定后端执行 OCR，返回 (text, confidence)。

    confidence 仅在 rapid 后端有值（平均置信度 0..1），其余后端为 -1（无置信度）。
    错误语义与 ocr_image 一致：后端未安装抛 RuntimeError，识别失败返回带前缀的文本。
    """
    backend = normalize_backend(backend)
    if backend == "tesseract":
        if not _PYTESSERACT:
            raise RuntimeError("未安装 pytesseract，无法使用 Tesseract OCR")
        try:
            return _tesseract_ocr(pil_image, lang), -1.0
        except Exception as e:
            return f"[OCR错误: {e}]", -1.0
    elif backend == "windows":
        if not _WINRT:
            raise RuntimeError("未安装 winrt，无法使用 Windows OCR")
        try:
            return _windows_ocr(pil_image), -1.0
        except Exception as e:
            return f"[OCR错误: {e}]", -1.0
    elif backend in (BACKEND_CPU, BACKEND_GPU):
        try:
            return _paddle_ocr(pil_image, lang, backend, det_limit=det_limit), -1.0
        except Exception as e:
            # 插件缺失等配置问题向上抛，便于设置页/测试弹窗展示
            msg = str(e)
            if "未安装" in msg or "插件" in msg:
                raise RuntimeError(msg) from e
            return f"[OCR错误: {e}]", -1.0
    elif backend == "rapid":
        if not _RAPID:
            raise RuntimeError("未安装 rapidocr_onnxruntime，无法使用 RapidOCR")
        try:
            return _rapid_ocr_full(pil_image, det_limit)
        except Exception as e:
            return f"[OCR错误: {e}]", -1.0
    return f"[OCR错误: 不支持的 backend '{backend}']", -1.0


def ocr_image(
    pil_image: Image.Image,
    lang: str = "chi_sim",
    backend: str = "tesseract",
    det_limit: int = 0,
) -> str:
    """按指定后端执行 OCR（仅返回文本）"""
    text, _ = ocr_image_extended(
        pil_image, lang=lang, backend=backend, det_limit=det_limit
    )
    return text
