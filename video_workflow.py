"""视频工作流（AIGC）：商品 → 文案 → 图片 + 配音 → 视频

对应岗位 JD：AIGC 工作流 / Agent 搭建（电商内容创作）。
Coze / Dify / n8n / ComfyUI 都是这套流程的"可视化版"，本质是同一个 pipeline。

流程：
  ① 文案：DeepSeek 生成短视频口播稿
  ② 图片：ComfyUI（本地 SD1.5）生成商品图
  ③ 配音：edge-tts 合成口播音频
  ④ 合成：ffmpeg 把「商品图 + 字幕 + 音频」合成竖屏视频

运行：
    python video_workflow.py "一款保温杯"
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import edge_tts

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-pro"
VOICE = "zh-CN-XiaoxiaoNeural"
FONT = "msyh.ttc"
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")  # 本地 ComfyUI（Docker 里用 host.docker.internal）
COMFY_OUTPUT = "E:/ComfyUI_windows_portable/ComfyUI/output"
CKPT = "v1-5-pruned-emaonly.safetensors"


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip())


def generate_script(product):
    """① 文案：DeepSeek 写一段 15 秒口播稿，每句一行。"""
    messages = [
        {"role": "system", "content": "你是电商短视频策划。为给定产品写一段 15 秒口播稿：3 句短句，每句一行，直接输出，不要序号、不要标题、不要多余解释。"},
        {"role": "user", "content": f"产品：{product}\n\n请写口播稿（每句一行）："},
    ]
    payload = {"model": MODEL, "messages": messages}
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
    )
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def generate_image_prompt(product):
    """中文商品名 → 英文图片提示词（SD 模型英文理解强、中文弱）。"""
    messages = [
        {"role": "system", "content": "把下面的中文商品名翻译成英文图片生成提示词，描述商品外观（材质/形状/颜色），15 词以内，直接输出英文。"},
        {"role": "user", "content": product},
    ]
    payload = {"model": MODEL, "messages": messages}
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
    )
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _comfy_workflow(prompt):
    """构造 ComfyUI txt2img 工作流（API 格式）。"""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": f"{prompt}, product photo, white background, high quality", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality, distorted", "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 20, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "product", "images": ["8", 0]}},
    }


def generate_image(product):
    """② 图片：调 ComfyUI API 生成商品图，返回图片完整路径。"""
    prompt = generate_image_prompt(product)  # 中文商品 → 英文提示词
    print(f"     英文提示词：{prompt}")
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=json.dumps({"prompt": _comfy_workflow(prompt)}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        pid = json.loads(r.read().decode())["prompt_id"]

    for _ in range(120):
        time.sleep(2)
        with urllib.request.urlopen(f"{COMFY_URL}/history/{pid}", timeout=30) as r:
            hist = json.loads(r.read().decode())
        if pid in hist and hist[pid].get("outputs"):
            img = hist[pid]["outputs"]["9"]["images"][0]
            return os.path.join(COMFY_OUTPUT, img["filename"])
    raise TimeoutError("ComfyUI 生成图片超时")


def generate_voice(text, out_path):
    """③ 配音：edge-tts 合成口播音频。"""
    async def _run():
        await edge_tts.Communicate(text, VOICE).save(out_path)
    asyncio.run(_run())


def escape_drawtext(t):
    """ffmpeg drawtext 文本转义。"""
    return (t.replace("\\", "\\\\").replace(":", "\:")
             .replace("'", "\'").replace("%", "\%").replace(",", "\,"))


def make_video(lines, image_path, voice_path, out_path):
    """④ 合成：商品图（居中）+ 字幕（底部）+ 音频 → 竖屏 mp4。"""
    dt = []
    y = 1180
    for line in lines:
        esc = escape_drawtext(line)
        dt.append(
            f"drawtext=fontfile={FONT}:text='{esc}':fontsize=44:"
            f"fontcolor=white:x=(w-text_w)/2:y={y}:"
            f"box=1:boxcolor=black@0.6:boxborderw=12"
        )
        y += 110
    drawtexts = ",".join(dt)

    fc = (
        f"[1:v]scale=760:760[img];"
        f"[0:v][img]overlay=x=(W-w)/2:y=280[bg];"
        f"[bg]{drawtexts}[v]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x16213e:s=1080x1920:r=30",
        "-i", image_path,
        "-i", voice_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


if __name__ == "__main__":
    load_env()
    product = sys.argv[1] if len(sys.argv) > 1 else "一款保温杯"
    print(f"① 生成文案（产品：{product}）...")
    lines = generate_script(product)
    for ln in lines:
        print(f"     {ln}")

    print("② 生成商品图（ComfyUI）...")
    image_path = generate_image(product)
    print(f"     {image_path}")

    voice_path = "voice.mp3"
    print("③ 合成配音...")
    generate_voice("".join(lines), voice_path)

    out_path = "output.mp4"
    print("④ 合成视频...")
    make_video(lines, image_path, voice_path, out_path)
    print(f"✅ 完成：{out_path}")
