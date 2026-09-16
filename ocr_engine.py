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

# PaddleOCR（可选）
_PADDLE = False
_PADDLE_IMPORT_ERROR = ""
try:
    # 兼容 paddle 与新版 protobuf 的冲突
    os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
    # 关闭 oneDNN/MKLDNN，避免某些 CPU 上报 Unimplemented 错误
    os.environ["FLAGS_use_mkldnn"] = "0"
    from paddleocr import PaddleOCR
    _PADDLE = True
except Exception as e:
    _PADDLE_IMPORT_ERROR = str(e)
    _PADDLE = False

# RapidOCR（可选）
try:
    from rapidocr_onnxruntime import RapidOCR
    _RAPID = True
except ImportError:
    _RAPID = False


SUPPORTED_BACKENDS = ["tesseract", "windows", "paddle", "rapid"]

# PaddleOCR 初始化较慢，按语言缓存实例
_paddle_ocr_cache = {}


def _to_paddle_lang(lang: str) -> str:
    """把 Tesseract 风格语言代码映射为 PaddleOCR 的语言代码"""
    lang_lower = lang.lower().split("_")[0]
    mapping = {
        "chi": "ch",
        "zh": "ch",
        "jpn": "japan",
        "ja": "japan",
        "eng": "en",
        "en": "en",
        "fr": "french",
        "de": "german",
        "ko": "korean",
        "korean": "korean",
    }
    return mapping.get(lang_lower, "ch")


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


def _get_paddle_ocr(lang: str) -> "PaddleOCR":
    """获取/缓存对应语言的 PaddleOCR 实例"""
    if not _PADDLE:
        raise RuntimeError("未安装 paddleocr")
    paddle_lang = _to_paddle_lang(lang)
    if paddle_lang not in _paddle_ocr_cache:
        _paddle_ocr_cache[paddle_lang] = PaddleOCR(lang=paddle_lang)
    return _paddle_ocr_cache[paddle_lang]


def _paddle_ocr(pil_image: Image.Image, lang: str) -> str:
    """使用 PaddleOCR 识别"""
    ocr = _get_paddle_ocr(lang)
    # 第一次使用时会自动下载模型，可能需要联网
    result = ocr.ocr(np.array(pil_image))
    if not result:
        return ""
    # 兼容不同版本返回结构
    lines = result[0] if isinstance(result, list) and result else []
    if not lines:
        return ""
    texts = []
    for line in lines:
        if line and len(line) >= 2:
            # line 结构：[box, (text, confidence)]
            texts.append(line[1][0])
    return "\n".join(texts).strip()


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


def ocr_image_extended(pil_image: Image.Image, lang: str = "chi_sim", backend: str = "tesseract", det_limit: int = 0) -> tuple:
    """按指定后端执行 OCR，返回 (text, confidence)。

    confidence 仅在 rapid 后端有值（平均置信度 0..1），其余后端为 -1（无置信度）。
    错误语义与 ocr_image 一致：后端未安装抛 RuntimeError，识别失败返回带前缀的文本。
    """
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
    elif backend == "paddle":
        if not _PADDLE:
            msg = _PADDLE_IMPORT_ERROR or "未安装 paddleocr"
            raise RuntimeError(f"无法使用 PaddleOCR: {msg}")
        try:
            return _paddle_ocr(pil_image, lang), -1.0
        except Exception as e:
            return f"[OCR错误: {e}]", -1.0
    elif backend == "rapid":
        if not _RAPID:
            raise RuntimeError("未安装 rapidocr_onnxruntime，无法使用 RapidOCR")
        try:
            return _rapid_ocr_full(pil_image, det_limit)
        except Exception as e:
            return f"[OCR错误: {e}]", -1.0
    return f"[OCR错误: 不支持的 backend '{backend}']", -1.0


def ocr_image(pil_image: Image.Image, lang: str = "chi_sim", backend: str = "tesseract", det_limit: int = 0) -> str:
    """按指定后端执行 OCR（仅返回文本）"""
    text, _ = ocr_image_extended(pil_image, lang=lang, backend=backend, det_limit=det_limit)
    return text
