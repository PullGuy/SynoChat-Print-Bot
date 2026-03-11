#!/usr/bin/env python3
"""
SynoChat Print Bot  v2
======================
接收 Synology Chat 发来的图片/PDF，经确认后发送到打印机打印。

新功能：
  1. 确认环节 —— 收到文件后生成有趣的「暗号词」，回复暗号才打印
  2. 打印选项 —— 支持双面/翻转边/横纵向组合，可用简码或自然语言指定
  3. 待打印队列超时自动清理（默认 5 分钟）

架构：
  手机 -> SynoChat -> Outgoing Webhook -> 本程序 -> CUPS 打印机
"""

import os, sys, re, json, time, random, logging, tempfile, shutil, threading, subprocess
import struct, zlib
import requests
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from dataclasses import dataclass, field

# ─── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("print_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── 配置 ────────────────────────────────────────────────────────────────────
def load_config():
    p = Path(__file__).parent / "config.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}

cfg = load_config()

SYNOCHAT_TOKEN     = cfg.get("synochat_token",        os.getenv("SYNOCHAT_TOKEN", ""))
SYNOCHAT_INCOMING  = cfg.get("synochat_incoming_url",  os.getenv("SYNOCHAT_INCOMING_URL", ""))
SYNAS_BASE_URL     = cfg.get("synas_base_url",         os.getenv("SYNAS_BASE_URL", "http://your-nas-ip:5000"))
SYNAS_USER         = cfg.get("synas_user",             os.getenv("SYNAS_USER", ""))
SYNAS_PASS         = cfg.get("synas_pass",             os.getenv("SYNAS_PASS", ""))
PRINTER_NAME       = cfg.get("printer_name",           os.getenv("PRINTER_NAME", ""))
LISTEN_HOST        = cfg.get("listen_host",            "0.0.0.0")
LISTEN_PORT        = int(cfg.get("listen_port",        8765))
MAX_FILE_MB        = int(cfg.get("max_file_mb",        50))
CONFIRM_TIMEOUT    = int(cfg.get("confirm_timeout_sec", 300))   # 等待确认超时（秒）
BOT_USER_ID        = cfg.get("bot_user_id", "7")
ALLOWED_EXT        = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp"}

# ─── PDF 页数获取 ─────────────────────────────────────────────────────────────
def get_pdf_pages(filepath: str) -> int:
    """快速统计 PDF 页数，不依赖第三方库。"""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        count = 0
        i = 0
        while True:
            pos = data.find(b"/Type", i)
            if pos == -1:
                break
            chunk = data[pos:pos+20].replace(b" ", b"").replace(b"\n", b"").replace(b"\r", b"")
            if chunk.startswith(b"/Type/Page") and not chunk.startswith(b"/Type/Pages"):
                count += 1
            i = pos + 1
        return count if count > 0 else 0
    except Exception as e:
        log.warning(f"获取 PDF 页数失败: {e}")
        return 0

def get_file_info(filepath: str, file_name: str) -> str:
    """返回文件简要信息，PDF 显示页数，图片显示大小。"""
    suffix = Path(file_name).suffix.lower()
    size_kb = Path(filepath).stat().st_size / 1024
    if suffix == ".pdf":
        pages = get_pdf_pages(filepath)
        page_str = f"{pages} 页" if pages > 0 else "页数未知"
        return f"PDF · {page_str} · {size_kb:.0f} KB"
    else:
        return f"图片 · {size_kb:.0f} KB"

# ─── 打印完成通知 ─────────────────────────────────────────────────────────────
def notify_when_done(job_name: str, channel: str, user_id: str):
    """后台线程：轮询 CUPS 队列，完成后通知用户。"""
    def _watch():
        max_wait = 300
        interval = 5
        elapsed  = 0
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            try:
                r = subprocess.run(["lpstat", "-o", job_name],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode != 0 or job_name not in r.stdout:
                    reply_chat("✅ 打印完成！文件已全部输出。", channel, user_id)
                    log.info(f"打印完成: {job_name}")
                    return
            except Exception:
                pass
        reply_chat("⚠️ 打印任务超过 5 分钟，请检查打印机状态。", channel, user_id)
        log.warning(f"打印完成等待超时: {job_name}")
    threading.Thread(target=_watch, daemon=True).start()


# ─── 有趣的确认暗号词库 ──────────────────────────────────────────────────────
# 每次从各组随机各取一个，拼成「形容词 + 名词」，独特且易于朗读输入
_ADJ = [
    "慵懒的", "暴躁的", "高冷的", "迷糊的", "挑剔的", "无敌的",
    "哲学的", "神秘的", "嗜睡的", "元气满满的", "懒洋洋的", "傲娇的",
    "呆萌的", "吃货界的", "深沉的", "咕咕叫的",
]
_NOUN = [
    "熊猫", "柴犬", "猫咪", "仓鼠", "企鹅", "章鱼",
    "拿铁", "芝士", "珍珠奶茶", "炸鸡", "松饼", "抹茶",
    "河豚", "羊驼", "鸭嘴兽", "独角兽", "龙虾", "松鼠",
    "刺猬", "水豚", "蜥蜴", "帝企鹅",
]

def generate_codeword() -> str:
    """生成随机暗号，例如：慵懒的熊猫"""
    return random.choice(_ADJ) + random.choice(_NOUN)


# ─── 打印选项 ─────────────────────────────────────────────────────────────────
@dataclass
class PrintOptions:
    copies: int = 1
    duplex: str = "one-sided"           # one-sided | two-sided-long-edge | two-sided-short-edge
    orient: str = "portrait"            # portrait | landscape
    color:  str = "monochrome"          # auto | color | monochrome（默认黑白）

    def cups_args(self) -> list:
        """转换为 lp -o 参数列表"""
        args = [
            "-o", f"sides={self.duplex}",
            "-o", f"orientation-requested={'4' if self.orient == 'landscape' else '3'}",
        ]
        if self.color == "color":
            args += ["-o", "ColorModel=RGB"]
        elif self.color == "monochrome":
            args += ["-o", "ColorModel=Gray"]
        return args

    def summary(self) -> str:
        duplex_map = {
            "one-sided":            "单面",
            "two-sided-long-edge":  "双面·翻长边",
            "two-sided-short-edge": "双面·翻短边",
        }
        color_map = {"auto": "自动色彩", "color": "彩色", "monochrome": "黑白"}
        return (
            f"份数 {self.copies} ｜ "
            f"{'横向' if self.orient == 'landscape' else '纵向'} ｜ "
            f"{duplex_map.get(self.duplex, self.duplex)} ｜ "
            f"{color_map.get(self.color, self.color)}"
        )
    
    def options_display(self) -> str:
        duplex_map = {
            "one-sided":            ("1", "单面"),
            "two-sided-long-edge":  ("2", "双面长边"),
            "two-sided-short-edge": ("3", "双面短边"),
        }
        orient_map = {
            "portrait":  ("1", "纵向"),
            "landscape": ("2", "横向"),
        }
        d_code, d_name = duplex_map.get(self.duplex, ("?", self.duplex))
        o_code, o_name = orient_map.get(self.orient, ("?", self.orient))
        color_name = "黑白" if self.color == "monochrome" else "彩色" if self.color == "color" else "自动"
        code = f"{d_code}{o_code}"
        return (
            f"当前选项：{d_name} · {o_name} · {color_name} · {self.copies}份\n"
            f"──────────────\n"
            f"单双面打印：1单，2翻长边，3翻短边\n" 
            f"打印方向：1纵向，2横向  份数：x2=2份"
            f"色彩：2彩色，3黑白（默认）\n"
        )


# ── 简码解析 ──────────────────────────────────────────────────────────────────
#
#  简码由 2～4 位数字组成，各位含义：
#
#  位1（双面）：  1=单面  2=双面·翻长边  3=双面·翻短边
#  位2（方向）：  1=纵向  2=横向
#  位3（色彩）：  1=自动  2=彩色  3=黑白        （可省略）
#  份数：         x3 或 3份                       （独立，不在简码内）
#
#  示例：
#    21    → 双面·翻长边·纵向（常用！）
#    22    → 双面·翻长边·横向
#    12    → 单面·横向
#    213   → 双面·翻长边·纵向·黑白
#    x3 21 → 3份，双面·翻长边·纵向

_CODE_RE = re.compile(r'\b([123])([12])([123])?\b')

def parse_options(text: str) -> PrintOptions:
    opt = PrintOptions()

    # 份数
    m = re.search(r'[xX×*](\d+)|(\d+)\s*份', text)
    if m:
        opt.copies = max(1, min(int(m.group(1) or m.group(2)), 20))

    # 简码（优先）
    m = _CODE_RE.search(text)
    if m:
        d = m.group(1)
        if d == "1":
            opt.duplex = "one-sided"
        elif d == "2":
            opt.duplex = "two-sided-long-edge"
        elif d == "3":
            opt.duplex = "two-sided-short-edge"
        opt.orient = "landscape" if m.group(2) == "2" else "portrait"
        if m.group(3):
            opt.color = {"1": "auto", "2": "color", "3": "monochrome"}[m.group(3)]

    # 自然语言（与简码可叠加，自然语言优先级更高）
    if "单面" in text:
        opt.duplex = "one-sided"
    if "双面" in text or "两面" in text:
        opt.duplex = "two-sided-long-edge"
    if "翻短" in text or "短边" in text:
        opt.duplex = "two-sided-short-edge"
    if "翻长" in text or "长边" in text:
        if "双面" in text or "两面" in text or opt.duplex != "one-sided":
            opt.duplex = "two-sided-long-edge"
    if "横" in text:
        opt.orient = "landscape"
    if "纵" in text or "竖" in text:
        opt.orient = "portrait"
    if "彩色" in text or "彩印" in text:
        opt.color = "color"
    if "黑白" in text or "灰度" in text:
        opt.color = "monochrome"

    return opt


# ─── 待确认队列 ───────────────────────────────────────────────────────────────
@dataclass
class PendingJob:
    codeword:   str
    file_path:  str      # 下载到本地的临时路径（确认期间不删除）
    file_name:  str
    channel:    str
    username:   str
    options:    PrintOptions
    created_at: float = field(default_factory=time.time)
    pending_confirm: bool  = False

_pending: dict = {}       # channel_id -> PendingJob
_lock = threading.Lock()

def _expire_loop():
    """后台线程：每 30 秒清理超时任务"""
    while True:
        time.sleep(30)
        now = time.time()
        expired = []
        with _lock:
            for ch, job in list(_pending.items()):
                if now - job.created_at > CONFIRM_TIMEOUT:
                    expired.append((ch, _pending.pop(ch)))
        for ch, job in expired:
            _cleanup_job(job)
            reply_chat(
                f"⏰ 「{job.file_name}」等待超时，打印任务已自动取消\n"
                f"需要打印请重新发送文件",
                ch, None
            )
            log.info(f"超时取消: {job.file_name}")

threading.Thread(target=_expire_loop, daemon=True).start()

def _cleanup_job(job: PendingJob):
    try:
        p = Path(job.file_path)
        if p.exists():
            p.unlink()
        parent = p.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception as e:
        log.warning(f"清理临时文件失败: {e}")


# ─── Synology 会话 ────────────────────────────────────────────────────────────
class SynoSession:
    def __init__(self):
        self.session = requests.Session()
        self.sid = None

    def login(self):
        r = self.session.get(
            f"{SYNAS_BASE_URL}/webapi/auth.cgi",
            params={"api": "SYNO.API.Auth", "version": "3", "method": "login",
                    "account": SYNAS_USER, "passwd": SYNAS_PASS,
                    "session": "FileStation", "format": "sid"},
            timeout=15, verify=False,
        )
        data = r.json()
        if data.get("success"):
            self.sid = data["data"]["sid"]
            log.info("Synology DSM 登录成功")
            return True
        log.error(f"Synology 登录失败: {data}")
        return False

    def download_file(self, url: str, dest: str) -> bool:
        headers = {"Cookie": f"id={self.sid}"} if self.sid else {}
        try:
            r = self.session.get(url, headers=headers, stream=True, timeout=60, verify=False)
            r.raise_for_status()
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
                    size += len(chunk)
                    if size > MAX_FILE_MB * 1024 * 1024:
                        log.warning("文件超出大小限制，中止")
                        return False
            log.info(f"下载完成: {dest} ({size/1024:.1f} KB)")
            return True
        except Exception as e:
            log.error(f"下载失败: {e}")
            return False

syno = SynoSession()


# ─── CUPS 打印 ────────────────────────────────────────────────────────────────
def print_file(filepath: str, options: PrintOptions) -> str:
    """提交打印任务，成功返回任务名（如 '7080D-12'），失败返回空字符串。"""
    cmd = ["lp"]
    if PRINTER_NAME:
        cmd += ["-d", PRINTER_NAME]
    cmd += ["-n", str(options.copies)]
    cmd += options.cups_args()
    cmd += ["--", filepath]
    log.info(f"打印命令: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            output = r.stdout.strip()
            log.info(f"打印成功: {output}")
            # 解析任务名，格式如：request id is 7080D-12 (1 file(s))
            m = re.search(r'is (\S+)', output)
            return m.group(1) if m else PRINTER_NAME
        log.error(f"打印失败: {r.stderr.strip()}")
        return ""
    except FileNotFoundError:
        log.error("未找到 lp 命令，请确认 CUPS 已安装")
        return ""
    except Exception as e:
        log.error(f"打印异常: {e}")
        return ""


# ─── 回复 SynoChat ────────────────────────────────────────────────────────────
def reply_chat(text: str, channel_id: str = None, user_id: str = None):
    if not SYNOCHAT_TOKEN:
        log.warning("未配置 SYNOCHAT_TOKEN")
        return
    try:
        url = f"{SYNAS_BASE_URL}/webapi/entry.cgi?api=SYNO.Chat.External&method=chatbot&version=2&token={SYNOCHAT_TOKEN}"
        payload = {"text": text}
        if user_id:
            payload["user_ids"] = [int(user_id)]
        elif channel_id:
            payload["channel_id"] = int(channel_id)
        r = requests.post(
            url,
            data={"payload": json.dumps(payload)},
            timeout=10,
            verify=False,
        )
        log.info(f"回复结果: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.error(f"回复失败: {e}")


# ─── 帮助文本 ─────────────────────────────────────────────────────────────────
HELP_TEXT = """📖 打印机器人使用说明

① 发送文件（PDF / JPG / PNG / TIFF / BMP）
可在消息文字中附带打印选项：

② 打印选项简码（位1·位2·位3）

```
位1 双面  1=单面  2=双面翻长边  3=双面翻短边
位2 方向  1=纵向  2=横向
位3 色彩  1=自动  2=彩色  3=黑白（可省略）
```

示例：
  `21`   双面翻长边·纵向（最常用）
  `22`   双面翻长边·横向
  `12`   单面·横向
  `213`  双面翻长边·纵向·黑白
  `x3 21` 3份·双面·纵向

也支持自然语言：双面、横向、黑白、彩色、3份

③ 确认暗号
收到文件后 Bot 会给你一个有趣的暗号
→ 回复暗号 ✅ 确认打印
→ 回复「取消」❌ 放弃打印
→ 5 分钟内不确认自动取消"""


# ─── 核心处理逻辑 ─────────────────────────────────────────────────────────────
def handle_payload(payload: dict):
    log.info(f"Webhook: {json.dumps(payload, ensure_ascii=False)[:400]}")

    if SYNOCHAT_TOKEN and payload.get("token") != SYNOCHAT_TOKEN:
        log.warning("Token 验证失败，忽略")
        return

    file_url  = payload.get("file_url", "").strip()
    file_name = payload.get("file_name", "file").strip()
    post_id   = payload.get("post_id", "").strip()
    username  = payload.get("username", "用户")
    channel   = payload.get("channel_id", "")
    user_id   = payload.get("user_id", "")
    text      = payload.get("text", "").strip()

    # 机器人模式下没有 file_url，用 post_id 拼接路径
    if not file_url and post_id and Path(file_name).suffix.lower() in ALLOWED_EXT:
        file_url = f"{SYNAS_BASE_URL}/webapi/entry.cgi?api=SYNO.FileStation.Download&version=2&method=download&path=/chat/@ChatWorking/uploads/{BOT_USER_ID}/{post_id}&mode=download"
    # ══════════════════════════════════════════════
    # A. 纯文字消息
    # ══════════════════════════════════════════════
    if not file_url:
        if not text:
            return

        tl = text.lower().strip()

        # 帮助
        if tl in ("帮助", "help", "?", "？", "使用说明", "怎么用"):
            reply_chat(HELP_TEXT, channel, user_id)
            return

        # 取消
        if tl in ("取消", "cancel", "算了", "不打了", "no", "不要"):
            with _lock:
                job = _pending.pop(channel or user_id, None)
            if job:
                _cleanup_job(job)
                reply_chat(f"🚫 已取消「{job.file_name}」的打印任务", channel, user_id)
            else:
                reply_chat("当前没有待确认的打印任务～", channel, user_id)
            return

        # 确认暗号
        # 确认暗号 / 修改选项循环
        job_key = channel or user_id
        with _lock:
            job = _pending.get(job_key)

        if job:
            tl2 = text.strip().lower()

            # ── 取消 ──
            if tl2 in ("取消", "cancel", "算了", "不打了", "no", "不要"):
                with _lock:
                    _pending.pop(job_key, None)
                _cleanup_job(job)
                reply_chat(f"🚫 已取消「{job.file_name}」的打印任务", channel, user_id)
                return

            # ── 已解锁后：直接输入选项码修改 ──
            if job.pending_confirm:
                if tl2 in ("是", "确认", "yes", "ok", "好"):
                    # 确认打印
                    with _lock:
                        _pending.pop(job_key, None)
                    reply_chat(
                        f"✅ 开始打印「{job.file_name}」\n📋 {job.options.options_display()}",
                        channel, user_id,
                    )
                    job_name = print_file(job.file_path, job.options)
                    _cleanup_job(job)
                    if job_name:
                        printer = f"「{PRINTER_NAME}」" if PRINTER_NAME else "默认打印机"
                        reply_chat(f"🖨️ 已发送到{printer}，打印完成后会通知您～", channel, user_id)
                        notify_when_done(job_name, channel, user_id)
                    else:
                        reply_chat("❌ 打印失败，请检查打印机状态", channel, user_id)
                else:
                    # 尝试解析为新选项
                    new_options = parse_options(text)
                    job.options = new_options
                    job.created_at = time.time()
                    reply_chat(
                        f"📋 选项已更新\n"
                        f"{new_options.options_display()}\n\n"
                        f"回复「是」确认打印 ｜ 继续输入选项码修改 ｜ 回复「取消」放弃",
                        channel, user_id,
                    )
                return

            # ── 未解锁：需要先输入暗号 ──
            parts = text.strip().split(None, 1)
            codeword_input = parts[0] if parts else ""
            option_input   = parts[1] if len(parts) > 1 else ""

            if codeword_input == job.codeword:
                job.pending_confirm = True
                job.created_at = time.time()
                if option_input:
                    new_options = parse_options(option_input)
                    job.options = new_options
                reply_chat(
                    f"🔓 暗号正确！\n\n"
                    f"{job.options.options_display()}\n\n"
                    f"回复「是」确认打印 ｜ 输入选项码修改 ｜ 回复「取消」放弃",
                    channel, user_id,
                )
            else:
                hint = job.codeword[0]
                reply_chat(
                    f"❌ 暗号不对哦～（提示：开头是「{hint}」）\n"
                    f"回复「取消」可放弃打印",
                    channel, user_id,
                )
            return

        # 无待任务
        reply_chat(
            "📄 请先发送图片或 PDF 文件，我来帮您打印！\n发送「帮助」查看详细说明",
            channel, user_id,
        )
        return

    # ══════════════════════════════════════════════
    # B. 收到文件附件
    # ══════════════════════════════════════════════
    suffix = Path(file_name).suffix.lower()
    if suffix not in ALLOWED_EXT:
        reply_chat(
            f"❌ 不支持的格式：{suffix}\n支持：PDF / JPG / PNG / TIFF / BMP",
            channel, user_id,
        )
        return

    # 替换旧的待确认任务
    job_key = channel or user_id
    with _lock:
        old = _pending.pop(job_key, None)
    if old:
        _cleanup_job(old)
        reply_chat("♻️ 已替换之前的待打印任务", channel, user_id)

    # 解析打印选项
    options = parse_options(text)

    # 下载文件（持久临时目录，等确认后再删）
    tmp_dir = tempfile.mkdtemp(prefix="synoprint_")
    dest = os.path.join(tmp_dir, file_name)

    if not syno.sid:
        syno.login()

    reply_chat(f"⬇️ 正在接收「{file_name}」...", channel, user_id)
    if not syno.download_file(file_url, dest):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        reply_chat("❌ 文件接收失败，请检查 NAS 连接或重新发送", channel, user_id)
        return

    # 生成暗号并入队
    codeword = generate_codeword()
    file_info = get_file_info(dest, file_name)
    job = PendingJob(
        codeword=codeword, file_path=dest, file_name=file_name,
        channel=job_key, username=username, options=options,
    )
    with _lock:
        _pending[job_key] = job

    expire_min = CONFIRM_TIMEOUT // 60
    reply_chat(
        f"📋 打印预览\n"
        f"文件：{file_name}\n"
        f"信息：{file_info}\n"
        f"\n{options.options_display()}\n"
        f"\n"
        f"🔑 确认打印，请回复暗号：{codeword}\n"
        f"🚫 回复「取消」放弃 ｜ ⏰ {expire_min} 分钟内有效",
        channel, user_id,
    )


# ─── HTTP Server ──────────────────────────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"HTTP {self.address_string()} - {fmt % args}")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            with _lock:
                count = len(_pending)
            self.wfile.write(json.dumps({
                "status": "ok", "service": "synochat-print-bot-v2",
                "pending_jobs": count,
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.rstrip("/") != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        ctype  = self.headers.get("Content-Type", "")

        try:
            if "application/json" in ctype:
                payload = json.loads(body)
            else:
                payload = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        except Exception as e:
            log.error(f"解析失败: {e}")
            self.send_response(400)
            self.end_headers()
            return

        # 立即 200，再异步处理
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"text":""}')

        try:
            handle_payload(payload)
        except Exception as e:
            log.exception(f"处理异常: {e}")


# ─── 入口 ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("SynoChat Print Bot v2 启动")
    log.info(f"监听: {LISTEN_HOST}:{LISTEN_PORT}/webhook")
    log.info(f"打印机: {PRINTER_NAME or '(系统默认)'}")
    log.info(f"确认超时: {CONFIRM_TIMEOUT}s")
    log.info("=" * 60)

    if SYNAS_USER and SYNAS_PASS:
        syno.login()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("服务停止")

if __name__ == "__main__":
    main()
