import pytest

from video_to_summary.polishers.llm import (
    LLMTranscriptPolisher,
    _clean_polished_text,
    _default_system_prompt,
)


class FakeTranscriptResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.segments = []


def test_clean_polished_text_does_not_remove_normal_entities() -> None:
    normal_text = "恒指Beta是正值0.02，冯小刚电影。华润啤酒业绩稳健。"
    assert _clean_polished_text(normal_text) == normal_text


def test_clean_polished_text_removes_advisor_artifacts() -> None:
    text = (
        "[Advisor consultation #1]\n"
        "## 现状判断\n"
        "some advice\n"
        "[End of advisor consultation #1]\n"
        "正常文本"
    )
    assert _clean_polished_text(text) == "正常文本"


def test_light_prompt_is_shorter_than_default() -> None:
    light = _default_system_prompt("light")
    default = _default_system_prompt("default")
    assert len(light) < len(default)
    assert "advisor" not in light.lower()


def test_polisher_preserves_common_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    key_entities = [
        "恒指正贝塔",
        "住房公积金",
        "保利发展",
        "华润啤酒",
        "分众传媒",
        "海吉亚",
        "牛来",
        "泡泡玛特",
        "肯德基",
        "可口可乐",
    ]

    class FakeMessage:
        content = (
            "恒指Beta是正值0.02。\n"
            "住房公积金修订条例扩大使用范围。\n"
            "保利发展涨2.7%，中海物业涨0.75%。\n"
            "华润啤酒业绩稳健，分众传媒平稳。\n"
            "海吉亚表现抗跌，牛来电影票房千万。\n"
            "泡泡玛特、肯德基、海底捞都有IP联动。\n"
            "可口可乐雪碧加茶，西贝面对预制菜争议。\n"
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):  # type: ignore
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore
            self.chat = type("chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    polisher = LLMTranscriptPolisher(api_key="sk-test", model="test-model")
    transcript = FakeTranscriptResult("some transcript")
    result = polisher.polish(transcript, title="test")

    preserved = sum(1 for entity in key_entities if entity in result.text)
    assert preserved >= len(key_entities) * 0.8


def test_clean_polished_text_preserves_numbers() -> None:
    text = (
        "恒指Beta是正值0.02，保利发展涨2.7%，中海物业涨0.75%。\n"
        "华润啤酒营收增1.2%，经营现金流增5.6%，股息为原先97折。\n"
        "A股主要指数大跌：上证指数跌2.6%，深证成指跌5%，创业板指跌6%，科创50跌7.5%，跌停约150家。\n"
        "华润双鹤并购利尔化学溢价约1.5倍，上半年净利润约50亿元。\n"
    )
    cleaned = _clean_polished_text(text)
    assert cleaned == text.strip()
    for token in ["0.02", "2.7%", "0.75%", "1.2%", "5.6%", "97折", "2.6%", "5%", "6%", "7.5%", "150家", "1.5倍", "50亿元"]:
        assert token in cleaned


def test_polisher_preserves_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    key_numbers = [
        "0.02", "2.7%", "0.75%", "1.2%", "5.6%", "97折",
        "2.6%", "5%", "6%", "7.5%", "150家", "1.5倍", "50亿元",
    ]

    class FakeMessage:
        content = (
            "恒指Beta是正值0.02，保利发展涨2.7%，中海物业涨0.75%。\n"
            "华润啤酒营收增1.2%，经营现金流增5.6%，股息为原先97折。\n"
            "A股主要指数大跌：上证指数跌2.6%，深证成指跌5%，创业板指跌6%，科创50跌7.5%，跌停约150家。\n"
            "华润双鹤并购利尔化学溢价约1.5倍，上半年净利润约50亿元。\n"
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):  # type: ignore
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore
            self.chat = type("chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    polisher = LLMTranscriptPolisher(api_key="sk-test", model="test-model")
    transcript = FakeTranscriptResult("some transcript")
    result = polisher.polish(transcript, title="test")

    preserved = sum(1 for num in key_numbers if num in result.text)
    assert preserved == len(key_numbers)
