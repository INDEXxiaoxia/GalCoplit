# GalCoplit

基于 PyQt6 的 Windows 桌面程序：陪你一起看 Galgame 剧情——实时 OCR 屏幕文字、按句数生成剧情总结、记录游玩时长。

## 主要功能

- **游戏 / 线路档案**：每个游戏可创建多条线路，数据按线路隔离，支持继续上次游玩。
- **框选区域持续 OCR**：在游戏画面上框出文字区域，持续截图识别。
- **OCR 后端可切换**：RapidOCR、Tesseract、Windows OCR、PaddleOCR。
- **剧情总结**：自动按句数触发，或手动模式随时总结；时间线查看与编辑。
- **关键信息**：自动提取伏笔、线索、人物、设定；角色卡片可拖动排序。
- **游玩时长留档**：自动记录每次游玩时间。
- **深色 / 浅色主题**：设置中实时切换。

## 运行环境

- Windows 10 / Windows 11
- Python 3.10+

## 快速开始（源码）

```bash
git clone https://github.com/INDEXxiaoxia/GalCoplit.git
cd GalCoplit
pip install -r requirements.txt
copy config.example.json config.json
```

编辑 `config.json`，填入你的 OpenAI 兼容 API Key、接口地址和模型名，然后：

```bash
python main.py
```

或双击 `start.bat`。

### 配置说明

复制 [config.example.json](config.example.json) 为 `config.json`（该文件不会被提交）。常用字段：

| 参数 | 说明 |
| --- | --- |
| `api_key` | OpenAI 兼容 API 密钥 |
| `base_url` | 接口地址，需以 `/v1` 结尾 |
| `model` | 模型名 |
| `ocr_backend` | `rapid`（推荐）/ `tesseract` / `windows` / `paddle` |
| `ocr_lang` | 如 `chi_sim`、`jpn`、`eng` |
| `summary_mode` | `auto` 或 `manual` |
| `summary_sentence_count` | 自动模式下累计多少句触发总结 |
| `theme` | `light` 或 `dark` |

> `config.json` 含密钥与本地状态，请勿提交到仓库。首次运行还会自动生成 `records.db`。

## 下载预编译版本

在 [Releases](https://github.com/INDEXxiaoxia/GalCoplit/releases) 下载 `GalCoplit.exe`，与 `config.json`（可从 `config.example.json` 复制）放在同一目录后运行。

## 打包

```bash
build.bat
```

产物：`dist/GalCoplit.exe`（约 130 MB）。请在干净的 `build_venv` 中构建；`requirements.txt` 已固定 `numpy<2` 与 `onnxruntime==1.18.1` 以保证 exe 内 RapidOCR 可用。

## 项目结构

```
GalCoplit/
├── main.py              # 主程序与界面
├── ocr_engine.py        # OCR 封装
├── llm_client.py        # LLM 调用
├── recorder.py          # SQLite 记录
├── theme.py             # 主题与样式
├── framecmp.py          # 帧对比辅助
├── config.example.json  # 配置示例
├── requirements.txt
├── GalCoplit.spec       # PyInstaller 规格
├── build.bat / start.bat
├── icon.ico
└── LICENSE              # GPL-3.0
```

## 许可证

本项目以 [GNU General Public License v3.0](LICENSE) 发布。

你可以自由使用、修改和分发本软件；若基于本项目制作衍生作品（含商用），**必须同样以 GPL-3.0（或兼容的强 copyleft 协议）开源**，并保留版权与许可声明。

## 注意事项

- 不要把带真实 `api_key` 的 `config.json` 或本地 `records.db` 上传到公开仓库。
- 框选区域、主题、上次游戏等本地状态会写入 `config.json`。
