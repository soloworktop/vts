from typing import Callable, Optional, Union

from video_to_summary.schemas import SummaryOutput
from video_to_summary.summarizers.base import Summarizer
from video_to_summary.utils import clean_advisor_artifacts


class OpenAISummarizer:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        template: Union[str, dict] = "通用",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.system_prompt = system_prompt
        self.template = template

    def summarize(
        self,
        transcript: str,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> str:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai package is required for summarization") from exc

        client_kwargs = {"api_key": self.api_key, "timeout": 120.0, "max_retries": 0}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url

        resolved_template = _resolve_template(self.template)
        client = OpenAI(**client_kwargs)
        messages = [
            {
                "role": "system",
                "content": self.system_prompt or _build_system_prompt(resolved_template),
            },
            {"role": "user", "content": _build_prompt(title, transcript, resolved_template)},
        ]

        from video_to_summary.cancel_utils import call_with_cancel

        def _call():
            try:
                completion = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )
                content = completion.choices[0].message.content or ""
                return _clean_summary(content.strip())
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        if cancel_check is None:
            return _call()

        return call_with_cancel(
            _call,
            cancel_check,
            on_cancel=client.close,
            cancel_log_msg="summarize cancelled by user",
        )


def _resolve_template(template: Union[str, dict]) -> dict:
    if isinstance(template, dict):
        return template
    return get_summary_template(template)


# 排版契约：所有模板共用，保证 LLM 输出的 Markdown 有一致的阅读体验
# （emoji 图标标题 / 加粗关键信息 / 引用块金句 / 受限的行内着色）。
# 行内着色只允许 color 一种声明，前端渲染器也只放行该形态的 span（双向对齐）。
# 拆成「格式段 + 元信息段」：结构化模板分支自带元信息规则，只追加格式段避免重复。
_STYLE_FORMAT = """排版要求（目标是阅读体验，须严格遵守）：
1. 仅输出 Markdown 正文：不要开场白、不要复述标题、不要任何元信息或分析过程
2. 第一行用引用块给出一句话核心结论，格式：> 🎯 <一句话>
3. 内容用 `- ` 列表承载；关键数字、人名/公司名、结论词用 **加粗**
4. 内容较多时用 `## ` 小标题分节，标题前加一个贴合语义的 emoji（如 ✨ 📌 💡 ⚠️ 🧠）
5. 引用值得记住的原话用 `> ` 引用块；多项并列对比可用 Markdown 表格
6. 需要警示/强调可用行内着色 <span style="color:#d1544a">文字</span>；style 仅允许 color 一种声明"""
# 元信息段：并入借鉴自 BiliNote BASE_PROMPT 的通用原则
# （专有名词/术语保留英文、数学公式用 LaTeX），与其余格式规则一起对全部模板生效。
_STYLE_META = """7. 全文中文；专有名词、技术术语、品牌名与人名保留英文原文，普通英文单词仍译为中文；视频提及的数学公式以 LaTeX 语法呈现；不要输出英文说明或任何 advisor / analysis / suggestions 标记"""

# 完整契约（prompt/自由式模板用，本身无其它元信息规则）
_STYLE_CONTRACT = _STYLE_FORMAT + "\n" + _STYLE_META


def _is_freeform(summary_template: dict) -> bool:
    """单一「要点」章节 = 极简自由式模板（如内置 default）：不约束章节结构，
    由排版契约保证可读性。用结构特征而非额外标志位判定，保证 DB 自定义往返后仍成立。"""
    sections = summary_template.get("sections") or []
    return len(sections) == 1 and sections[0] == "要点"


def _build_system_prompt(summary_template: dict) -> str:
    # 新模型：模板即一段提示词（内置 default 与全部自定义模板）
    if "prompt" in summary_template:
        instruction = (summary_template.get("prompt") or "总结视频文稿内容").strip()
        return f"你是一个专业的中文内容总结助手。任务：{instruction}\n\n{_STYLE_CONTRACT}"

    # 兼容旧结构（sections/hints 三层，来自旧示例代码或未迁移数据）
    sections = summary_template["sections"]
    optional_sections = summary_template.get("optional_sections", [])

    if _is_freeform(summary_template):
        instruction = (
            summary_template.get("hints", {}).get(sections[0]) or "总结视频文稿内容"
        ).strip()
        return f"你是一个专业的中文内容总结助手。任务：{instruction}。\n\n{_STYLE_CONTRACT}"

    section_lines = []
    for idx, section in enumerate(sections, start=1):
        hint = summary_template.get("hints", {}).get(section, "")
        if section in optional_sections:
            section_line = f"{idx}. {section}：{hint}（如内容涉及则整理，否则可省略）".strip("：")
        else:
            section_line = f"{idx}. {section}：{hint}".strip("：")
        section_lines.append(section_line)

    optional_note = ""
    if optional_sections:
        optional_note = f"其中 {', '.join(optional_sections)} 为可选章节；如转录内容未涉及，可直接省略。\n"

    return f"""你是一个专业的中文内容总结助手。你的任务是从口述转录中提取核心结论，不要记流水账。

输出必须严格按下列结构整理：
""" + "\n".join(section_lines) + f"""

重要规则：
1. 优先提炼结论，不要把口语重复转成冗长文本。
2. 同一含义最多合并为 1 条，不要重复罗列近似表述。
3. 保留关键数字、公司名、时间点。
4. 不要输出分析过程、英文说明或任何 advisor / analysis / suggestions 标记。
5. 仅输出最终总结内容。
{optional_note}
""" + _STYLE_FORMAT


def _build_prompt(title: str, transcript: str, summary_template: dict) -> str:
    max_chars = 12000
    if len(transcript) > max_chars:
        transcript = transcript[: max_chars // 2] + "\n...[truncated]...\n" + transcript[-max_chars // 2 :]

    section_names = "、".join(summary_template.get("sections") or [])
    freeform = "prompt" in summary_template or _is_freeform(summary_template)
    structure_req = (
        "- 直接输出可读性优先的 Markdown 总结\n"
        if freeform
        else f"- 严格按结构输出：{section_names}\n"
    )
    return (
        "请直接输出最终中文结构化总结，不要输出任何分析、建议或元信息。\n\n"
        f"Title: {title}\n\n"
        "Transcript:\n"
        f"{transcript}\n\n"
        "输出要求：\n"
        f"{structure_req}"
        "- 压缩冗余，只保留核心结论\n"
        "- 保留关键数字、公司名、时间点\n"
        "- 不要重复近似表述\n"
        "- 仅中文\n"
        "- 不要输出英文说明或分析过程\n"
    )


def _clean_summary(text: str) -> str:
    # 与 polisher 共用同一份 advisor 残留清理规则（utils.clean_advisor_artifacts）
    return clean_advisor_artifacts(text)


# 内置模板名统一用中文；"default" 是历史名，作为兼容别名仍可解析（存量设置/任务不受影响），
# 但不出现在模板列表中；创建/重试入口会把别名归一化为 DEFAULT_TEMPLATE_NAME。
DEFAULT_TEMPLATE_NAME = "通用"
TEMPLATE_ALIASES = {"default": DEFAULT_TEMPLATE_NAME}


def get_summary_template(template: str) -> dict:
    template_key = TEMPLATE_ALIASES.get(template or "", template or "") or DEFAULT_TEMPLATE_NAME
    if template_key not in SUMMARY_TEMPLATES:
        raise ValueError(f"Unknown summary template: {template_key}")
    return SUMMARY_TEMPLATES[template_key]


SUMMARY_TEMPLATES = {
    # 内置模板借鉴 BiliNote（https://github.com/JefferyHcool/BiliNote）的笔记风格集
    # （其 backend/app/gpt/prompt_builder.py 的 minimal/detailed/academic/tutorial/
    # xiaohongshu/life_journal/task_oriented/business/meeting_minutes 九种风格），
    # 改写为本项目的 v4 单段提示词模型：提示词只描述「总结什么、怎么组织」，
    # 排版体验由 _STYLE_CONTRACT 统一保证。自定义模板（SQLite）与之同构：
    # {"prompt": "..."}，创建/删除走模板管理页（DB 自定义 > 内置同名）。
    #
    # 字典声明序即展示序（template_store.list_templates 按此序返回，自定义模板追加在后）：
    # 按视频使用场景的覆盖面与使用频率从高到低——通用与摘要先行（任意视频），
    # 学习类（教程/学术/会议）居中，创作与垂类（商业/小红书/生活/任务）靠后。
    # default = 通用兜底：不限定视频类型，只给「提炼什么、怎么组织」的普适指引
    # （排版交给 _STYLE_CONTRACT）。提示词经 A/B 实测：比素版「总结视频文稿内容」
    # 主题分节更清晰、信息密度更高，且不给输出强加特定场景结构。
    "通用": {
        "prompt": "用通用笔记风格总结：优先提炼核心结论与关键观点，不要流水账；按主题分节组织，先总后分；"
                  "保留关键数字、人名/公司名、案例与可直接执行的建议；同一观点只讲一次，篇幅适中、宁精勿滥",
    },
    "精简笔记": {
        "prompt": "用精简风格总结：只提炼最重要的核心要点（不超过 8 条），每条一句话，宁缺毋滥；省略寒暄、铺垫、广告与重复内容，让人 30 秒看完就知道视频讲了什么",
    },
    "详细笔记": {
        "prompt": "用详细笔记风格总结：尽可能完整地记录视频内容，按视频叙事顺序分节，覆盖每个部分的主要观点、论据、例子与细节讨论；重要的事实、数据、结论与建议都要保留，不要过度压缩",
    },
    "教程笔记": {
        "prompt": "以教程笔记风格总结：面向想跟着操作的观众，按操作步骤组织内容，详细记录关键点、命令/参数/代码、工具名称与重要结论步骤，标注易错点与注意事项，结尾给一份可勾选的上手清单",
    },
    "学术笔记": {
        "prompt": "以学术报告风格总结：正式、严谨、结构化，按「研究背景与问题 → 方法与思路 → 关键发现与论据 → 结论与意义 → 局限与展望」组织；视频中的数学公式必须保留并以 LaTeX 语法呈现",
    },
    "会议纪要": {
        "prompt": "以会议纪要风格总结：正式、客观、只记录事实与结论，按「会议主题与背景 → 讨论要点（分议题归类） → 达成的结论与决议 → 待办事项（事项 + 负责方） → 遗留问题」组织；口语讨论去噪后按议题归类，不抒情、不发散",
    },
    "商业分析": {
        "prompt": "以商业分析报告风格总结：正式、精准、数据导向，按「业务概览 → 商业模式与产品要点 → 关键数据与里程碑 → 机会与风险 → 结论与研判」组织；关键数字加粗，多项对比优先用表格呈现",
    },
    "小红书笔记": {
        "prompt": "以小红书爆款笔记风格总结：口语化、有感染力，善用「划重点」「宝藏」「手把手」「建议收藏」「干货满满」「万万没想到」等爆款词与悬念式表达，用感叹号营造情绪；开头一句话要抓人，正文突出干货与获得感。所有信息必须来自视频原文，不得编造",
    },
    "生活随笔": {
        "prompt": "以生活随笔风格总结：第一人称视角，情感化、轻松自然地记录视频中的故事、经历与感悟，突出能引发共鸣的瞬间；打动人的原话用引用块保留为金句",
    },
    "任务清单": {
        "prompt": "以任务导向风格总结：面向要落实的行动，从视频中提炼目标、方法与行动项，用可勾选的 checkbox 列表（- [ ]）整理待办事项，写清先做什么、怎么做、做到什么程度算完成",
    },
}


__all__ = [
    "OpenAISummarizer",
    "get_summary_template",
    "SUMMARY_TEMPLATES",
    "DEFAULT_TEMPLATE_NAME",
    "TEMPLATE_ALIASES",
]
