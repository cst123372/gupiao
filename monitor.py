"""B站UP主动态监控 —— GitHub Actions 单次运行版"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

CST = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "last_seen.json"
CONFIG_FILE = BASE_DIR / "config.json"


# ── 日志 ──
def log(msg: str) -> None:
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── 配置 ──
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 状态 ──
def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── B站 API ──
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})


def bili_api_get(url: str, params: dict = None) -> dict | None:
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        if resp.status_code == 412:
            log("B站风控412，稍后自动重试")
            return None
        if resp.status_code != 200:
            log(f"HTTP {resp.status_code}: {url}")
            return None
        data = resp.json()
        if data.get("code") != 0:
            log(f"API错误 code={data.get('code')}")
            return None
        return data
    except Exception as e:
        log(f"请求异常: {e}")
        return None


def fetch_space_dynamics(uid: str, offset: str = "") -> list[dict]:
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": uid, "offset": offset}
    data = bili_api_get(url, params)
    if not data:
        return []
    return data.get("data", {}).get("items", [])


# ── 格式化 ──
def format_dynamic(item: dict, up_name: str) -> str:
    id_str = item.get("id_str", "")
    modules = item.get("modules", {})
    author = modules.get("module_author", {})
    name = author.get("name", up_name)
    major = modules.get("module_dynamic", {}).get("major", {})
    desc = modules.get("module_dynamic", {}).get("desc", {})
    text = desc.get("text", "") if desc else ""
    dyn_type = item.get("type", "DYNAMIC_TYPE_WORD")

    # 话题
    topic = modules.get("module_dynamic", {}).get("topic")
    topic_info = f"  #{topic['name']}#" if topic and topic.get("name") else ""

    lines = [f"**{name}** 发布了新动态{topic_info}", ""]

    if dyn_type == "DYNAMIC_TYPE_AV":
        archive = major.get("archive", {})
        title = archive.get("title", "")
        bvid = archive.get("bvid", "")
        lines.append(f"🎬 **{title}**")
        if bvid:
            lines.append(f"🔗 https://www.bilibili.com/video/{bvid}")
        if text:
            lines.append(f"📝 {text}")

    elif dyn_type == "DYNAMIC_TYPE_DRAW":
        draw_items = major.get("draw", {}).get("items", [])
        if text:
            lines.append(f"📝 {text}")
        lines.append(f"🖼️ 发布了{len(draw_items)}张图片")

    elif dyn_type == "DYNAMIC_TYPE_WORD":
        clean = re.sub(r"<[^>]+>", "", text)
        lines.append(f"📝 {clean}" if clean else "📝 [文字动态]")

    elif dyn_type == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig", {})
        orig_name = "未知"
        if orig:
            oa = orig.get("modules", {}).get("module_author", {})
            orig_name = oa.get("name", "未知")
        lines.append(f"🔄 转发了 **{orig_name}** 的动态")
        if text:
            lines.append(f"📝 {text}")

    else:
        clean = re.sub(r"<[^>]+>", "", text) if text else ""
        lines.append(f"📝 {clean}" if clean else "📝 [新动态]")

    lines.append("")
    lines.append(f"🕐 {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
    if id_str:
        lines.append(f"🔗 https://t.bilibili.com/{id_str}")

    return "\n".join(lines)


# ── 企业微信 ──
def send_wecom(webhook_url: str, content: str) -> bool:
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("errcode") == 0:
            return True
        log(f"企微推送失败: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"企微连接失败: {e}")
        return False


# ── 检查单个UP主 ──
def check_up(uid: str, name: str, webhook: str, state: dict) -> dict:
    last_id = state.get(uid, "0")
    items = fetch_space_dynamics(uid)

    if not items:
        return state

    # 筛选新动态
    new_items = [it for it in items if str(it.get("id_str", "0")) > str(last_id)]
    if not new_items:
        return state

    new_items.sort(key=lambda x: int(x.get("id_str", "0")))
    newest_id = new_items[-1].get("id_str", last_id)

    log(f"[{name}] 发现 {len(new_items)} 条新动态")

    if len(new_items) == 1:
        msg = format_dynamic(new_items[0], name)
        if send_wecom(webhook, msg):
            log(f"[{name}] 推送成功 -> {newest_id}")
            state[uid] = newest_id
    else:
        header = f"**{name}** 发布了 {len(new_items)} 条新动态\n---\n\n"
        combined = header
        for item in new_items:
            combined += format_dynamic(item, name) + "\n\n---\n\n"

        if len(combined) > 4000:
            combined = header
            for i, item in enumerate(new_items, 1):
                id_str = item.get("id_str", "")
                combined += f"**{i}.** https://t.bilibili.com/{id_str}\n"
                if len(combined) > 3800:
                    combined += f"...还有 {len(new_items) - i} 条\n"
                    break

        if send_wecom(webhook, combined):
            log(f"[{name}] 批量推送成功 -> {newest_id}")
            state[uid] = newest_id

    return state


# ── 主入口 ──
def main():
    config = load_config()
    webhook = os.environ.get("WECOM_WEBHOOK", "")
    if not webhook:
        log("未设置 WECOM_WEBHOOK 环境变量")
        sys.exit(1)

    up_masters = config.get("up_masters", [])
    if not up_masters:
        log("config.json 中未配置UP主")
        sys.exit(1)

    state = load_state()
    log(f"开始检查 {len(up_masters)} 位UP主...")

    for master in up_masters:
        uid = master["uid"]
        name = master.get("name", uid)
        state = check_up(uid, name, webhook, state)
        time.sleep(2)  # UP主之间间隔，避免限流

    save_state(state)
    log("本轮完成")


if __name__ == "__main__":
    main()
