# Basic Pitch 音频转 MIDI

AstrBot 插件：接收聊天中上传的音乐音频，调用 Spotify Basic Pitch 转录为标准 MIDI，然后将 `.mid` 文件发送回当前会话。

## 功能

- 支持 `.mp3`、`.wav`、`.flac`、`.m4a`、`.ogg`。
- 仅在执行 `/midi` 时转换：可在同一消息附加音频，或引用一条音频消息后发送 `/midi`。
- 转录计算在线程中执行，不阻塞 AstrBot 的异步消息循环。
- 缓存 Basic Pitch 模型，避免每个任务重复加载。
- 限制下载和处理的文件大小，临时文件会在任务结束后清理。

## 安装

本插件**必须使用 Python 3.11** 的 AstrBot 运行环境。`basic-pitch==0.4.0` 所依赖的 TensorFlow 2.15 没有可用的 Python 3.12 或 3.13 Windows 发行包，因此 Python 3.12/3.13 环境无法安装或运行此插件。

通过 AstrBot WebUI 的插件管理页安装插件时，AstrBot 会自动检查并安装根目录 `requirements.txt` 中缺失的依赖。手动将本目录放入 AstrBot 的 `data/plugins/` 后，请在 AstrBot 使用的 Python 3.11 环境中手动安装：

```bash
python -m pip install --prefer-binary -r requirements.txt --index-url https://pypi.org/simple
```

本插件使用当前可用的 `basic-pitch` 版本。针对部分 Linux/Python 3.11 环境中 Librosa 与 Numba 的 `guvectorize` 编译问题，插件在转换时会使用 `soundfile` 或 `audioread` 解码音频、使用 SciPy 重采样，绕过 Basic Pitch 调用 `librosa.load()` 时可能触发的 Numba JIT 错误。因此无需额外固定 NumPy、Numba、llvmlite 或 Librosa 的版本；请使用与 AstrBot 相同的 Python 环境安装依赖。

```bash
python -m pip install --upgrade pip wheel --index-url https://pypi.org/simple --no-cache-dir
python -m pip install --prefer-binary -r requirements.txt --index-url https://pypi.org/simple --no-cache-dir
```

请固定使用 Python 3.11，例如 `D:\桌面\Code\AstrBot\.venv311\Scripts\python.exe`。首次转录会加载随 `basic-pitch` 安装的模型，可能需要较长初始化时间。不要额外安装 `basic-pitch[tf]`：本插件会使用安装包附带的 TFLite 模型，以避开 TensorFlow SavedModel 的兼容性问题。

### 共享环境依赖冲突

AstrBot 的插件通常共用同一个 Python 环境。本插件不再固定 NumPy、Numba、llvmlite 或 Librosa 版本，以减少与其他音频、科学计算和机器学习插件的依赖冲突。若服务器仍无法解码特定格式，请先确认系统具备相应音频解码能力；`soundfile` 不支持的格式会自动尝试由 `audioread` 解码。

## 配置

AstrBot WebUI 中可设置 `max_file_size_mb`，默认 `50`。超出限制的附件会被拒绝下载和转换。

## 使用

1. 在 QQ、Telegram 或其他支持文件消息的适配器中发送支持的音频文件。
2. 引用这条音频消息并发送 `/midi`，或在携带音频附件的消息中发送 `/midi`。
3. 插件回复“正在转换音频，请稍候……”。
4. 成功后，机器人发送名为 `原文件名_YYYYMMDD_HHMMSS.mid` 的 MIDI 文件。

## 限制

- Basic Pitch 更适合清晰的单乐器或较简单音乐素材；复杂混音、人声和非音乐音频可能产生空 MIDI 或不准确结果。
- `File` 组件的实际发送能力取决于平台适配器；QQ OneBot（aiocqhttp）通常是优先支持目标。
- 服务器需要足够的 CPU、内存和磁盘临时空间。较长音频会显著增加推理时间。

## 参考文献与致谢

本插件基于以下开源项目和技术：

1. Spotify Basic Pitch：音频到 MIDI 的自动音乐转录模型与工具。
   - 论文：Jesse Engel et al., *Reducing the Need for Music Transcription Data Using Weakly Supervised Learning*，ISMIR 2020。
   - 项目地址：https://github.com/spotify/basic-pitch
   - Python 包文档：https://basicpitch.spotify.com/
2. AstrBot：提供插件运行时、消息处理和文件发送能力。
   - 项目地址：https://github.com/AstrBotDevs/AstrBot
3. TensorFlow Lite：本插件在 Windows 环境中使用的 Basic Pitch 推理后端。
   - 项目地址：https://www.tensorflow.org/lite

Basic Pitch、AstrBot、TensorFlow 及其依赖项均遵循各自项目声明的许可证。本插件不改变或重新分发这些第三方项目的许可证条款；使用时请同时遵守其许可证和版权声明。

## 开源协议

本插件自身采用 GNU Affero General Public License v3.0（AGPL-3.0-only），详见 [`LICENSE`](LICENSE)。如修改本插件并通过网络向用户提供服务，请遵守 AGPL v3 对相应源代码提供的要求。

## 开发验证

修改插件后，在 AstrBot WebUI 的插件管理页面重新加载插件。测试时分别覆盖：合法音频、损坏文件、超过大小限制的文件、无音符音频，以及不支持扩展名。
