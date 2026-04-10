import asyncio
import base64
import inspect
import io
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEFAULT_CONFIG = {
    "trigger": "摸摸",
    "interval": 0.05,
    "avatar_offset_x": 0,
    "avatar_offset_y": 0,
    "avatar_anchor": "center",
    "avatar_scale": 1.0,
}

QQ_AVATAR_URLS = [
    "https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640&img_type=jpg",
    "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
]


@register("astrbot_plugin_petpet", "codex", "摸头杀 petpet GIF 插件", "1.0.0")
class PetPetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.base_dir = Path(__file__).resolve().parent
        self.assets_dir = self.base_dir / "data" / "petpet"
        self.output_dir = self.assets_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config if config is not None else {}
        self._ensure_config_defaults()
        self._cleanup_task: Optional[asyncio.Task] = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_gif_loop())
        logger.info("[petpet] 插件已加载，定时清理任务已启动")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        text = self._get_text(event).strip()
        if not text:
            return

        if text.startswith(".petset"):
            if not await self._is_admin_or_owner(event):
                yield event.plain_result("你没有权限使用该命令（仅机器人管理员或群主）。")
                return
            msg = self._handle_petset(text)
            yield event.plain_result(msg)
            return

        trigger = str(self._config_get("trigger", DEFAULT_CONFIG["trigger"])).strip()
        if not (text == trigger or text.startswith(trigger + " ")):
            return

        if not self._assets_ready():
            logger.error("[petpet] 缺少素材，请检查 data/petpet/frame0.png ~ frame4.png")
            yield event.plain_result("petpet 素材缺失，请联系管理员检查插件目录下 data/petpet/frame0~4.png")
            return

        target_user_id = self._resolve_target_user_id(event, text, trigger)
        if not target_user_id:
            yield event.plain_result("无法识别用户，请稍后再试。")
            return

        avatar = await self._resolve_avatar(event, target_user_id)
        if avatar is None:
            yield event.plain_result("未能获取目标头像，请稍后再试。")
            return

        try:
            gif_path = self._build_petpet_gif(avatar, float(self._config_get("interval", DEFAULT_CONFIG["interval"])))
        except Exception:
            logger.exception("[petpet] 生成 GIF 失败")
            yield event.plain_result("生成 petpet GIF 失败，请稍后再试。")
            return

        yield self._image_result(event, gif_path)

    def _ensure_config_defaults(self):
        self._apply_config(self._normalized_config())

    def _normalized_config(self) -> dict:
        trigger = str(self._config_get("trigger", DEFAULT_CONFIG["trigger"])).strip() or DEFAULT_CONFIG["trigger"]
        try:
            interval = float(self._config_get("interval", DEFAULT_CONFIG["interval"]))
        except Exception:
            interval = DEFAULT_CONFIG["interval"]
        interval = max(0.02, min(1.0, interval))
        try:
            avatar_offset_x = int(float(self._config_get("avatar_offset_x", DEFAULT_CONFIG["avatar_offset_x"])))
        except Exception:
            avatar_offset_x = DEFAULT_CONFIG["avatar_offset_x"]
        try:
            avatar_offset_y = int(float(self._config_get("avatar_offset_y", DEFAULT_CONFIG["avatar_offset_y"])))
        except Exception:
            avatar_offset_y = DEFAULT_CONFIG["avatar_offset_y"]
        avatar_offset_x = max(-60, min(60, avatar_offset_x))
        avatar_offset_y = max(-60, min(60, avatar_offset_y))
        anchor = self._normalize_anchor(self._config_get("avatar_anchor", DEFAULT_CONFIG["avatar_anchor"]))
        try:
            avatar_scale = float(self._config_get("avatar_scale", DEFAULT_CONFIG["avatar_scale"]))
        except Exception:
            avatar_scale = DEFAULT_CONFIG["avatar_scale"]
        avatar_scale = max(0.3, min(3.0, avatar_scale))
        return {
            "trigger": trigger,
            "interval": interval,
            "avatar_offset_x": avatar_offset_x,
            "avatar_offset_y": avatar_offset_y,
            "avatar_anchor": anchor,
            "avatar_scale": avatar_scale,
        }

    @staticmethod
    def _normalize_anchor(value: Any) -> str:
        text = str(value).strip().lower()
        if text in {"右下", "右下角", "bottom_right", "right_bottom", "rb"}:
            return "bottom_right"
        return "center"

    def _apply_config(self, cfg: dict):
        target = self.config
        if isinstance(target, dict):
            target.update(cfg)
            return

        for key, value in cfg.items():
            try:
                target[key] = value
                continue
            except Exception:
                pass
            try:
                setattr(target, key, value)
            except Exception:
                continue

    def _config_get(self, key: str, default: Any = None) -> Any:
        target = self.config
        getter = getattr(target, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        if isinstance(target, dict):
            return target.get(key, default)
        return getattr(target, key, default)

    def _handle_petset(self, text: str) -> str:
        m = re.match(r"^\.petset\s+(速度|指令|位置|头像位置|对齐|锚点|缩放|倍率)\s+(.+?)\s*$", text)
        if not m:
            return "用法：.petset 速度 0.06、.petset 指令 揉揉、.petset 位置 0 0、.petset 对齐 居中、.petset 缩放 1.2"
        key, value = m.group(1), m.group(2).strip()
        if key == "速度":
            try:
                interval = float(value)
            except Exception:
                return "速度必须是数字，例如：.petset 速度 0.06"
            if interval <= 0:
                return "速度必须大于 0。"
            self._apply_config({"interval": max(0.02, min(1.0, interval))})
            return f"已设置摸头速度（帧间隔）为 {self._config_get('interval', DEFAULT_CONFIG['interval']):.3f}s"
        if key in {"位置", "头像位置"}:
            parts = value.split()
            if len(parts) != 2:
                return "位置用法：.petset 位置 x y，例如 .petset 位置 0 0"
            try:
                offset_x = int(float(parts[0]))
                offset_y = int(float(parts[1]))
            except Exception:
                return "位置必须是数字，例如：.petset 位置 0 0"
            self._apply_config({"avatar_offset_x": max(-60, min(60, offset_x)), "avatar_offset_y": max(-60, min(60, offset_y))})
            return (
                f"已设置头像位置偏移为 x={self._config_get('avatar_offset_x', 0)}, "
                f"y={self._config_get('avatar_offset_y', 0)}"
            )
        if key in {"对齐", "锚点"}:
            anchor = self._normalize_anchor(value)
            self._apply_config({"avatar_anchor": anchor})
            anchor_text = "右下角" if anchor == "bottom_right" else "居中"
            return f"已设置头像对齐方式为：{anchor_text}"
        if key in {"缩放", "倍率"}:
            try:
                scale = float(value)
            except Exception:
                return "缩放必须是数字，例如：.petset 缩放 1.2"
            if scale <= 0:
                return "缩放必须大于 0。"
            scale = max(0.3, min(3.0, scale))
            self._apply_config({"avatar_scale": scale})
            return f"已设置头像缩放倍率为 {self._config_get('avatar_scale', 1.0):.2f}x"
        if not value:
            return "触发词不能为空。"
        self._apply_config({"trigger": value})
        return f"已设置触发词为：{self._config_get('trigger', DEFAULT_CONFIG['trigger'])}"

    async def _is_admin_or_owner(self, event: AstrMessageEvent) -> bool:
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        role = str(getattr(sender, "role", "")).lower()
        if role in {"owner", "admin"}:
            return True

        for name in ("is_admin", "is_owner"):
            checker = getattr(event, name, None)
            if callable(checker):
                try:
                    ret = checker()
                    if inspect.isawaitable(ret):
                        ret = await ret
                    if bool(ret):
                        return True
                except Exception:
                    continue
        return False

    def _resolve_target_user_id(self, event: AstrMessageEvent, text: str, trigger: str) -> Optional[str]:
        explicit_uid = self._extract_explicit_qq_uid(text, trigger)
        if explicit_uid:
            return explicit_uid

        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or []
        at_uid = None
        reply_uid = None

        for seg in chain:
            t = seg.__class__.__name__.lower()
            if t == "at" and at_uid is None:
                at_uid = self._first_attr(seg, ("qq", "user_id", "id", "target"))
            if t in {"reply", "quote"} and reply_uid is None:
                reply_uid = self._first_attr(seg, ("user_id", "qq", "id", "target"))

        if reply_uid is None:
            raw = getattr(msg_obj, "raw_message", None)
            reply_uid = self._extract_reply_uid(raw)

        if at_uid:
            return str(at_uid)
        if reply_uid:
            return str(reply_uid)
        
        sender = getattr(msg_obj, "sender", None)
        sender_id = self._first_attr(sender, ("user_id", "id", "qq"))
        if sender_id:
            return str(sender_id)
        
        return None

    def _extract_explicit_qq_uid(self, text: str, trigger: str) -> Optional[str]:
        if not text.startswith(trigger):
            return None

        tail = text[len(trigger):].strip()
        if not tail:
            return None

        m = re.fullmatch(r"@?\s*(\d{5,12})", tail)
        if m:
            return m.group(1)
        return None

    def _extract_reply_uid(self, raw: Any) -> Optional[str]:
        if not isinstance(raw, dict):
            return None
        paths = [
            ("reply", "user_id"),
            ("reply", "sender_id"),
            ("reply", "sender", "user_id"),
            ("quote", "user_id"),
            ("quote", "sender", "user_id"),
            ("reference", "author", "id"),
        ]
        for p in paths:
            cur = raw
            ok = True
            for key in p:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur[key]
            if ok and cur:
                return str(cur)
        return None

    async def _resolve_avatar(self, event: AstrMessageEvent, user_id: str) -> Optional[Image.Image]:
        candidates = []
        for name in ("get_user_avatar", "get_avatar", "get_target_avatar", "get_sender_avatar"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    data = fn() if name == "get_sender_avatar" else fn(user_id)
                    if inspect.isawaitable(data):
                        data = await data
                    candidates.append(data)
                except Exception:
                    continue

        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        sender_uid = self._first_attr(sender, ("user_id", "id"))
        if sender and sender_uid and str(sender_uid) == str(user_id):
            for k in ("avatar", "avatar_url", "face", "icon"):
                v = getattr(sender, k, None)
                if v:
                    candidates.append(v)

        for data in candidates:
            img = self._to_image(data)
            if img is not None:
                return img.convert("RGBA")
        
        img = await self._download_qq_avatar(user_id)
        if img is not None:
            return img
        
        return None
    
    async def _download_qq_avatar(self, user_id: str) -> Optional[Image.Image]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            for url_template in QQ_AVATAR_URLS:
                url = url_template.format(user_id=user_id)
                try:
                    resp = await client.get(url, headers=headers, follow_redirects=True)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        img = Image.open(io.BytesIO(resp.content))
                        logger.info(f"[petpet] 从QQ头像API获取头像成功: {user_id}")
                        return img.convert("RGBA")
                except Exception as e:
                    logger.warning(f"[petpet] 获取头像失败 {url}: {e}")
        return None

    def _to_image(self, data: Any) -> Optional[Image.Image]:
        if data is None:
            return None
        if isinstance(data, Image.Image):
            return data
        if isinstance(data, (bytes, bytearray)):
            try:
                return Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                return None
        if isinstance(data, str):
            text = data.strip()
            if text.startswith("http://") or text.startswith("https://"):
                return None
            if text.startswith("data:image"):
                try:
                    raw = base64.b64decode(text.split(",", 1)[1])
                    return Image.open(io.BytesIO(raw)).convert("RGBA")
                except Exception:
                    return None
            p = Path(text)
            if p.exists() and p.is_file():
                try:
                    return Image.open(p).convert("RGBA")
                except Exception:
                    return None
        return None

    def _build_petpet_gif(self, avatar: Image.Image, interval: float) -> Path:
        canvas_size = (112, 112)
        try:
            avatar_scale = float(self._config_get("avatar_scale", DEFAULT_CONFIG["avatar_scale"]))
        except Exception:
            avatar_scale = DEFAULT_CONFIG["avatar_scale"]
        avatar_scale = max(0.3, min(3.0, avatar_scale))
        avatar_size = max(20, int(round(75 * avatar_scale)))
        avatar = avatar.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        offset_x = int(self._config_get("avatar_offset_x", DEFAULT_CONFIG["avatar_offset_x"]))
        offset_y = int(self._config_get("avatar_offset_y", DEFAULT_CONFIG["avatar_offset_y"]))
        anchor = self._normalize_anchor(self._config_get("avatar_anchor", DEFAULT_CONFIG["avatar_anchor"]))
        initial_base_x = canvas_size[0] - avatar_size
        initial_base_y = canvas_size[1] - avatar_size
        
        # Benisland 风格的 5 帧按压曲线：轻压 -> 重压 -> 回弹。
        motion_data = [
            (0.98, 0.98, 0, 0),
            (1.01, 0.91, -1, 4),
            (1.06, 0.82, 1, 8),
            (1.02, 0.92, -1, 5),
            (0.99, 0.99, 0, 1),
        ]
        
        frames = []
        for i in range(5):
            hand = Image.open(self.assets_dir / f"frame{i}.png").convert("RGBA")
            canvas = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
            
            sx, sy, ox, oy = motion_data[i]
            w = int(avatar_size * sx)
            h = int(avatar_size * sy)
            squeezed = avatar.resize((w, h), Image.Resampling.LANCZOS)

            if anchor == "bottom_right":
                base_x = initial_base_x
                base_y = initial_base_y
            else:
                base_x = (canvas_size[0] - w) // 2
                base_y = (canvas_size[1] - h) // 2

            x = base_x + ox + offset_x
            y = base_y + oy + offset_y
            
            canvas.paste(squeezed, (x, y))
            canvas = Image.alpha_composite(canvas, hand)
            
            frames.append(canvas.convert("P", palette=Image.Palette.ADAPTIVE))
        
        out_path = self.output_dir / f"petpet_{uuid.uuid4().hex}.gif"
        frames[0].save(
            out_path,
            save_all=True,
            append_images=frames[1:],
            duration=max(20, int(interval * 1000)),
            loop=0,
            optimize=False,
            disposal=2,
        )
        return out_path

    async def _cleanup_gif_loop(self):
        while True:
            try:
                self._cleanup_old_gifs(max_age_seconds=6 * 3600)
            except Exception:
                logger.exception("[petpet] 定时清理失败")
            await asyncio.sleep(3600)

    def _cleanup_old_gifs(self, max_age_seconds: int):
        now = time.time()
        for f in self.output_dir.glob("petpet_*.gif"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink(missing_ok=True)
            except Exception:
                continue

    def _assets_ready(self) -> bool:
        return all((self.assets_dir / f"frame{i}.png").exists() for i in range(5))

    def _image_result(self, event: AstrMessageEvent, path: Path):
        if hasattr(event, "make_result"):
            result = event.make_result()
            if hasattr(result, "image"):
                result.image(str(path))
                return result
        return event.image_result(str(path))

    def _get_text(self, event: AstrMessageEvent) -> str:
        v = getattr(event, "message_str", None)
        if isinstance(v, str):
            return v
        msg_obj = getattr(event, "message_obj", None)
        v2 = getattr(msg_obj, "message_str", "")
        return v2 if isinstance(v2, str) else ""

    @staticmethod
    def _first_attr(obj: Any, keys: tuple[str, ...]) -> Optional[Any]:
        if obj is None:
            return None
        for k in keys:
            v = getattr(obj, k, None)
            if v is not None and v != "":
                return v
        return None
