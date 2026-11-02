"""Render README demo images from demo data: python demo_assets.py"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

import agent_panel as ap
import icon_art


def demo_models() -> tuple[ap.PanelModel, ...]:
    bubble = ap.BubbleModel
    busy = ap.PanelModel(
        "codex", "C", "Codex", "购买额度运行",
        (("2小时13分钟", 0.0, "购买额度 59.8k"), ("3天4小时", 38.0, "")), 85.0,
        (bubble("a", "重构悬停面板", "编辑 hover_details.py", "C", True, "重构悬停面板气泡交互"),
         bubble("b", "补齐测试", "运行测试", "C", True),
         bubble("d", "README 文档", "刚完成", "G", False)),
        "paid", "Codex 重置 22%",
        "5 小时额度已用完，正在用购买的额度；2小时13分钟后恢复", "warn", quota_tones=("paid", ""),
        hint_now="Codex：重构悬停面板；另有 1 个 agent 在工作", hint_next="可先检查测试结果，再决定是否发布")
    idle = ap.PanelModel("grok", "X", "Grok", "空闲", (("2 小时", 100.0, "已到重置时间"),), None, (), "idle",
                         hint_now="当前未检测到生成中的 agent", hint_next="可准备下一项任务，或查看刚完成的结果")
    mimo = ap.PanelModel("mimo", "M", "Xiaomi MiMo", "空闲", (("套餐额度", 96.9, ""),), 0, (), "idle",
                         forecast="按近期速度可用到到期，预计剩余 82%", forecast_tone="ok", today_used=1.53e9,
                         hint_now="正在核对本机任务状态", hint_next="可整理下一步的验收项")
    return busy, idle, mimo


def render(destination: Path, scale: float = 1.5) -> Path:
    frames = []
    for model in demo_models():
        scene, moment = ap.Scene(), 0.0
        scene.set_model(model, moment)
        for _ in range(120):
            moment += 1 / 60
            scene.step(moment, None, False, False)
        frames.append(scene.render(scale))
    sheet = Image.new("RGBA", (sum(f.width for f in frames), max(f.height for f in frames)), (31, 52, 74, 255))
    x = 0
    for frame in frames:
        sheet.alpha_composite(frame, (x, 0))
        x += frame.width
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(destination, optimize=True)
    return destination


def render_icons(destination: Path) -> Path:
    """Illustrative figures using the restored tray icon style."""
    examples = [
        ("Codex 73", icon_art.ring_icon((83,73), "C")),
        ("Grok 42", icon_art.ring_icon((42,), "X")),
        ("Gemini 10", icon_art.ring_icon((24,10), "G")),
        ("Claude 48", icon_art.ring_icon((62,48), "A")),
        ("MiMo 97", icon_art.ring_icon((97,), "M")),
        ("Wait 120 hours", icon_art.waiting_icon(120, "C")),
        ("Active 86 TPS", icon_art.live_icon(86, "G")),
    ]
    width, column = 1160, 160
    sheet = Image.new("RGBA", (width, 218), (32, 31, 30, 255))
    draw = ImageDraw.Draw(sheet)
    title, label = ap.ui_font(18, "bold"), ap.ui_font(12)
    draw.text((20,12), "Tray states (sample data) - 16px enlarged 5x and icon sizes", font=title, fill=(240,238,230))
    for index,(name,icon) in enumerate(examples):
        x = 20 + index*column
        frame = icon.resize((16,16),Image.Resampling.LANCZOS)
        sheet.alpha_composite(frame.resize((80,80),Image.Resampling.NEAREST),(x+35,42))
        draw.text((x+80,129), name, font=label, fill=(205,201,194), anchor="mm")
        for j,size in enumerate((16,20,24,32)):
            actual = icon.resize((size,size),Image.Resampling.LANCZOS)
            sheet.alpha_composite(actual,(x+8+j*36,169-size))
            draw.text((x+8+j*36+size/2,183), str(size), font=label, fill=(148,145,140), anchor="mm")
    destination.parent.mkdir(parents=True,exist_ok=True)
    sheet.convert("RGB").save(destination,optimize=True)
    return destination


if __name__ == "__main__":
    print(render(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("assets") / "panel-demo.png"))
    if len(sys.argv) == 1:
        print(render_icons(Path(__file__).with_name("assets") / "icons-demo.png"))
