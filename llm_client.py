import json
import re
from typing import Optional
import requests


class LLMClient:
    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.api_key = config["api_key"]
        self.model = config["model"]

    def _chat(self, messages: list, temperature: float = 0.5, timeout: int = 120) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    def summarize(self, text: str, previous_summary: str = "", extra_prompt: str = "") -> str:
        system = (
            "你正在陪用户玩Galgame。请根据OCR识别出的游戏文本，结合之前的剧情总结，"
            "用中文总结当前剧情。总结要内容充实、篇幅适当加长，尽量完整保留以下有效信息："
            "出场人物及其名字、对话要点、行为动机与人物关系；重要事件的完整经过与前因后果；"
            "伏笔、线索以及对后续剧情可能有影响的细节；涉及的世界观、地点、组织等设定；"
            "以及明显的情感变化。宁可保留更多细节，也不要过度精简丢失线索。"
            "不要添加任何前缀或标题（如“剧情总结：”），直接输出总结内容。"
        )
        if extra_prompt:
            system += "\n\n" + extra_prompt

        user = ""
        if previous_summary:
            user += f"之前的剧情总结：\n{previous_summary}\n\n"
        user += f"最近识别到的游戏文本：\n{text}\n\n请生成一段内容充实、细节完整的中文剧情总结："

        return self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            timeout=60,
        )

    def generate_title(self, summary_text: str) -> str:
        """为剧情总结生成一个简短的段落标题（10 字以内）"""
        system = (
            "你是Galgame剧情记录助手。请为给定的剧情总结拟一个段落标题。"
            "要求：不超过10个汉字，凝练概括本段剧情重点，有画面感；"
            "不要任何标点、引号、序号或前缀，直接输出标题本身。"
        )
        user = f"剧情总结：\n{summary_text}\n\n请输出标题："
        title = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            timeout=60,
        )
        title = title.strip().splitlines()[0].strip().strip("\"'“”‘’《》【】")
        return title[:16]

    def extract_key_info(self, summary_text: str, raw_text: str = "", extra_prompt: str = "") -> list:
        """从总结和原始文本中提取关键信息。返回列表 [{content, category, detail}]"""
        system = (
            "你是Galgame剧情分析助手。请严格从剧情总结和原始文本中，提取真正关键的信息。"
            "关键信息仅分为三类：剧情线索、世界观设定、角色信息。"
            "剧情线索：只对后续剧情理解或事件发展有明确推动作用的重要伏笔、关键线索、核心事件走向。普通的场景铺垫、日常寒暄、情绪描写不要放入。"
            "世界观设定：作品中的核心规则、重要组织、关键地点、时代/背景设定等，必须是影响整个故事框架或后续理解的关键设定，不要收录零散的装饰性描述。"
            "角色信息：在本段中具有实际戏份或推动剧情作用的重要角色。对于角色信息，content 字段填写角色名（例如：小明），"
            "如果该角色有别名或另一个常用称呼，请在角色名后用括号补充，格式为：原名（别名），"
            "例如：由佳（ユカ）。不要包含任何描述、身份或说明；描述必须放在 detail 字段中。"
            "先对每条候选信息进行严格评估：如果它只是普通对话、场景描写、情绪渲染、无关紧要的细节，或对理解后续剧情没有实质帮助，就不要放入结果。"
            "只返回JSON数组，每个元素包含 content（内容）、category（分类：剧情线索/世界观设定/角色信息）、"
            "detail（角色信息时必填，其他可为空）。如果没有明显关键信息，返回空数组 []。"
        )
        if extra_prompt:
            system += "\n\n" + extra_prompt
        user = f"剧情总结：\n{summary_text}\n\n原始文本：\n{raw_text}\n\n请提取关键信息："
        try:
            text = self._chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                timeout=60,
            )
            return self._parse_key_info(text)
        except Exception:
            return []

    def match_character(self, name: str, candidates: list) -> Optional[int]:
        """使用 LLM 判断候选角色是否与已有角色为同一人，返回已有角色 id 或 None"""
        if not candidates:
            return None
        system = (
            "你是Galgame剧情分析助手。请判断候选角色是否与已有角色为同一人。"
            "规则：中文环境下，姓、名、全名、带别名的名字可能指向同一人；"
            "仅当非常确定是同一人时才匹配，不确定则返回 NO_MATCH。"
            "只返回纯文本：若匹配则只写已有角色的 id 数字，否则写 NO_MATCH。"
        )
        candidates_text = "\n".join(
            [f"id={c['id']}, name={c['name']}, info={c['info']}" for c in candidates]
        )
        user = f"候选角色：{name}\n\n已有角色：\n{candidates_text}\n\n请判断："
        try:
            text = self._chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
            )
            text = text.strip()
            if text == "NO_MATCH":
                return None
            # 优先匹配数字
            match = re.search(r"\d+", text)
            if match:
                char_id = int(match.group(0))
                for c in candidates:
                    if c["id"] == char_id:
                        return char_id
            # 兼容：模型直接写了名字
            for c in candidates:
                if c["name"] in text:
                    return c["id"]
        except Exception:
            pass
        return None

    def _parse_key_info(self, text: str) -> list:
        """尝试从 LLM 返回的文本中解析 JSON 数组"""
        # 去除可能的 markdown 代码块
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```")
        cleaned = cleaned.strip()
        if not cleaned:
            return []
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 再尝试提取方括号包裹的内容
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return []
            else:
                return []

        if isinstance(data, list):
            result = []
            for item in data:
                if isinstance(item, dict) and "content" in item:
                    result.append({
                        "content": str(item.get("content", "")),
                        "category": str(item.get("category", "其他")),
                        "detail": str(item.get("detail", "")),
                    })
            return result
        return []
