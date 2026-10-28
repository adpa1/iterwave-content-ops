"""小红书、知乎共用的二维码截图：验证图案，只保存二维码，不兜底整页。"""
from __future__ import annotations

import base64
import io
import math
import os
from pathlib import Path
import tempfile


def save_qr(image_bytes: bytes, output: Path, *, minimum_size: int = 0) -> bool:
    """检测二维码图案、解码内容；跳过 deeplink schema 的假 QR（baiduboxapp:// 等 App 内部唤起）。"""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    source = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    image = Image.new("RGBA", source.size, "white")
    image.alpha_composite(source)
    image = image.convert("RGB")
    image = ImageOps.expand(image, border=max(8, math.ceil(max(image.size) * 0.12)), fill="white")
    pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    detector = cv2.QRCodeDetector()
    # detectAndDecode：同时解码 QR 内容！deeplink schema（baiduboxapp://...）不是真登录 QR，跳过
    decoded, points, _ = detector.detectAndDecode(pixels)
    # 注意：points 是 numpy array 时不能用 `not points`（numpy truth value error），必须用 `is None`
    if (not decoded or points is None) and min(image.size) < 320:
        decoded, points, _ = detector.detectAndDecode(cv2.resize(
            pixels, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
        if points is not None:
            points = points / 2
    if not decoded or points is None:
        return False
    # --- 解码内容过滤：deeplink schema 不是真 passport 扫码登录 ---
    # 规则：如果 QR 内容以 http/https 开头 → 都是 web URL，放行（passport 真登录 URL 就是 https）
    # 否则含 deeplink 标记 → 跳过（baiduboxapp://、weixin:// 等 App 内部唤起）
    decoded_lower = decoded.lower()
    if not decoded_lower.startswith(("http://", "https://")):
        for marker in ("://", "baiduboxapp", "talos.invoke", "invoketalospage",
                       "rnplugin.creationcenter", "weixin://", "alipays://", "weibo://",
                       "snssdk", "douyin://", "kuaishou://", "xhslink"):
            if marker.lower() in decoded_lower:
                return False   # deeplink / App 唤起，跳过
    corners = points.reshape(-1, 2)
    left = max(0, math.floor(float(corners[:, 0].min())))
    top = max(0, math.floor(float(corners[:, 1].min())))
    right = min(image.width, math.ceil(float(corners[:, 0].max())) + 1)
    bottom = min(image.height, math.ceil(float(corners[:, 1].max())) + 1)
    if right <= left or bottom <= top:
        return False
    if min(right - left, bottom - top) < minimum_size:
        return False
    cropped = image.crop((left, top, right, bottom))
    padding = max(8, math.ceil(max(cropped.size) * 0.12))
    cropped = ImageOps.expand(cropped, border=padding, fill="white")
    if min(cropped.size) < 360:
        scale = 360 / min(cropped.size)
        cropped = cropped.resize(
            (round(cropped.width * scale), round(cropped.height * scale)),
            Image.Resampling.NEAREST)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = io.BytesIO()
    cropped.save(encoded, format="PNG")
    png_bytes = encoded.getvalue()
    try:
        if output.read_bytes() == png_bytes:
            return True
    except FileNotFoundError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(png_bytes)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


# 登录二维码 img/src 黑名单：deeplink schema（baiduboxapp://、weixin:// 等）或 App 内部 deeplink
# 伪装成登录 QR 但扫出来只是唤起 App，不是 passport 扫码登录。命中这些 src 的元素直接跳过。
_DEEPLINK_QR_MARKERS = (
    # ⚠️ 不要加 "://" —— 合法 web 登录 QR 的 src 也是 https://...（如百度 passport 带 sign 的真码），
    # "://" 会误拦！deeplink 检测用下面这些 App 专属 marker 精准拦截即可；
    # save_qr() 里还有一层图案解码后的 deeplink 检测（更精确，会识别 QR 内容而非 src）。
    "baiduboxapp",          # 百度系 App deeplink（百家号 passport 假码）
    "talos.invoke",         # 百度 talos deeplink
    "invokeTalosPage",
    "rnplugin.creationCenter",  # creationCenter 内部 deeplink
)


def _is_deeplink_qr(element) -> bool:
    """检查元素 src / data-* 属性是否含 deeplink schema（不是真 web 扫码登录）。"""
    try:
        src = element.get_attribute("src") or ""
        # 也查常见 data-* QR 属性（百度 passport 有时用 data-qr / data-sign）
        for attr in ("data-qr", "data-sign", "data-url", "data-schema", "data-href"):
            v = element.get_attribute(attr) or ""
            src += v
        src_lower = src.lower()
        # 过滤 data:image base64 里的 deeplink——base64 不会以 "://" 开头，
        # 但 base64 解码后可能含 "baiduboxapp"——这种情况后面 save_qr 图案识别也能扫到 deeplink 内容
        # 所以 src 层只过滤非 base64 的明文 src
        if not src.startswith("data:"):
            for marker in _DEEPLINK_QR_MARKERS:
                if marker.lower() in src_lower:
                    return True
        return False
    except Exception:
        return False


def capture_qr(page, output: Path, selectors: str, *, attempts: int = 3,
               minimum_size: int = 0) -> None:
    """遍历可见图片/画布及二维码容器，等待实际像素就绪后裁剪。
    自动跳过 deeplink schema 的假 QR（如 baiduboxapp:// deeplink）。

    selectors 里可嵌入 ``__api_url__:https://...`` 特殊标记——
    匹配到会先试 ``page.request.get`` 那个 URL 下载原始尺寸 QR PNG，
    适用于 passport.js 在 headless 里不渲染 DOM 的场景（如百家号、抖音）。"""
    candidates = [selector.strip() for selector in selectors.split(",") if selector.strip()]
    api_urls = [c.split(":", 1)[1].strip() for c in candidates
                if c.startswith("__api_url__:")]
    candidates = [c for c in candidates if not c.startswith("__api_url__:")]

    # --- 先试 API URL 下载（最高优先级，拿原始尺寸）---
    for api_url in api_urls:
        for attempt in range(attempts):
            try:
                resp = page.request.get(api_url, timeout=10000)
                if resp.status == 200:
                    image_bytes = resp.body()
                    if save_qr(image_bytes, output, minimum_size=minimum_size):
                        return
            except Exception:
                pass
            if attempt < attempts - 1:
                page.wait_for_timeout(500)

    # --- 再走 DOM selector 遍历 ---
    if not candidates:
        raise RuntimeError(
            f"__api_url__ 下载失败且无 DOM selector；"
            f"passport API ({api_urls}) 可能返回非 QR 内容。")
    leaf_selectors = [f"{selector} img, {selector} canvas" for selector in candidates]
    selector_list = leaf_selectors + candidates
    for attempt in range(attempts):
        seen = set()
        for selector in selector_list:
            try:
                elements = page.query_selector_all(selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if not element.is_visible():
                        continue
                    # --- deeplink 假 QR 过滤 ---
                    if _is_deeplink_qr(element):
                        continue
                    bounds = element.bounding_box()
                    if not bounds or min(bounds["width"], bounds["height"]) < 48:
                        continue
                    if max(bounds["width"], bounds["height"]) > 1200:
                        continue
                    # --- 获取图片字节优先级 ---
                    # 1) http(s) src: page.request.get 下载原始尺寸
                    # 2) data:image;base64,...: 直接 base64.b64decode 拿原始 PNG/JPEG（比 CSS 尺寸 screenshot 更可靠）
                    # 3) 其他情况（canvas 无 src、data:image 非 base64、SVG 等）: element.screenshot 裁剪
                    image_bytes = None
                    src = element.get_attribute("src") or ""
                    tag = (element.evaluate("el => el.tagName") or "").upper()
                    if tag == "IMG" and src.startswith(("http://", "https://")):
                        try:
                            resp = page.request.get(src, timeout=5000)
                            if resp.status == 200:
                                image_bytes = resp.body()
                        except Exception:
                            image_bytes = None
                    if image_bytes is None and src.startswith("data:image"):
                        try:
                            # data:image/png;base64,iVBOR... → 取逗号后部分 base64 decode
                            b64_part = src.split(",", 1)[-1] if "," in src else src
                            image_bytes = base64.b64decode(b64_part)
                        except Exception:
                            image_bytes = None
                    if image_bytes is None:
                        image_bytes = element.screenshot(timeout=3000, animations="disabled")
                    if image_bytes in seen:
                        continue
                    seen.add(image_bytes)
                    if save_qr(image_bytes, output, minimum_size=minimum_size):
                        return
                except Exception:
                    continue
        if attempt < attempts - 1:
            page.wait_for_timeout(800)
    raise RuntimeError("未找到可识别的登录二维码；页面可能尚未加载、被平台拦截或需要切换扫码登录")


def prepare_qr(page, output: Path, selectors: str, switch_selectors: str, *,
               minimum_size: int = 0) -> None:
    """已有可识别二维码时不点击切换控件，以免切回短信登录。

    selectors / switch_selectors 支持 Playwright locator 扩展语法
    （:has-text(), text= 等），不限于纯 CSS —— 用 locator 而非
    query_selector_all / wait_for_selector 解析，避免混了扩展语法就炸。
    """
    try:
        page.locator(f"{selectors}, {switch_selectors}").first.wait_for(
            state="visible", timeout=15000)
    except Exception:
        pass
    try:
        capture_qr(page, output, selectors, minimum_size=minimum_size)
        return
    except RuntimeError as error:
        missing = error
    # switch_selectors 为空则直接失败（不 try 空 selector 会抛）
    if switch_selectors:
        try:
            switches = page.locator(switch_selectors).all()
        except Exception:
            switches = []
        for element in switches:
            try:
                if not element.is_visible():
                    continue
            except Exception:
                continue
            element.click(timeout=3000)
            try:
                page.locator(selectors).first.wait_for(
                    state="visible", timeout=15000)
            except Exception:
                pass
            capture_qr(page, output, selectors, minimum_size=minimum_size)
            return
    raise missing
