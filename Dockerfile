# AIGC 视频工作流 · 容器化
FROM python:3.12-slim

# 1. 装 ffmpeg（视频合成依赖）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. 先复制依赖文件并安装（利用 Docker 层缓存：改代码不重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. 复制代码 + 中文字体
COPY video_workflow.py .
COPY msyh.ttc .

# 4. API key 运行时传入，不写死进镜像（安全）
# 运行：docker run --rm -e DEEPSEEK_API_KEY=sk-xxx -e COMFY_URL=http://host.docker.internal:8188 <image> "一款保温杯"
ENTRYPOINT ["python", "video_workflow.py"]
