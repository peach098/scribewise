"""Issue #46: the user's grammatical gender is guessed locally from their own messages, and a
wrong guess is worse than none ("no signal" asks the user)."""

from __future__ import annotations

import pytest

from scribe.style.grammatical_gender import forms_in, infer_grammatical_gender


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Я сделал, проверь", {"masculine"}),
        ("я сделала, проверь", {"feminine"}),
        ("Я уже отправил отчёт", {"masculine"}),
        ("Я вчера написала тебе", {"feminine"}),
        ("я не проверял", {"masculine"}),
        ("я тут файл приложила", set()),  # a noun first: the form is not right after «я»
        ("я тут приложила файл", {"feminine"}),
        ("Я отписался в задаче", {"masculine"}),
        ("Я отписалась в задаче", {"feminine"}),
        ("Я смог запустить", {"masculine"}),
        ("я смогла запустить", {"feminine"}),
        ("Я пришёл в офис", {"masculine"}),
        ("я пришла в офис", {"feminine"}),
        ("я нашёл ошибку", {"masculine"}),
        ("я помог Пете", {"masculine"}),
        ("Я готов созвониться", {"masculine"}),
        ("Я готова созвониться", {"feminine"}),
        ("Я рад", {"masculine"}),
        ("я сама посмотрю", {"feminine"}),
        ("я отошла на минуту", {"feminine"}),
        ("я сошла с ума", {"feminine"}),
        ("я обошла все тесты", {"feminine"}),
        ("я шла домой", {"feminine"}),
        ("я отошёл на минуту", {"masculine"}),
        ("я мол сделал", {"masculine"}),  # «мол» is a filler, not a form
        # mixed: one message with both forms counts for both
        ("Я сделал, а она сказала, что я ошиблась", {"masculine", "feminine"}),
    ],
)
def test_first_person_forms_are_found(text: str, expected: set[str]) -> None:
    assert forms_in(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Привет! Гляну сегодня",  # no gendered form
        "Мы сделали всё",  # plural
        "Пришла задача от Ани",  # verb without «я» agrees with «задача»
        "Готова новая версия",  # the adjective is about a thing
        "Готов отчёт по Q3",
        "Готов?",  # a question to the other person
        "Ты готова?",
        "Она сделала, а он проверил",  # third persons
        "Сделано, отправлю вечером",
        "John рад помочь",
        "I did it, I'm ready",  # other languages: no signal
        "Я в офисе",
        "Я и Петя сделали",
        # code review probes (issue #46): nouns, objects first, other forms
        "я тебе вопрос задам",
        "я запрос отправлю",
        "я коллег спрошу",
        "я канал создам",
        "я все дела закрою",
        "я материал посмотрю",
        "я числа проверю",
        "я правила обновлю",
        "я отдел спрошу",
        "я протокол пришлю",
        "я это не проверял",  # an object pronoun between: not counted (conservative)
        # spellings that leave the gender open
        "я сделал(а)",
        "я посмотрел/а",
        "Я сделал/сделала",
        "Готов(а) обсудить",
        "я готов(а)",
        "я сделал(-а) всё",
        "я упустил (а)",
        "я сделал / сделала",
        "я посмотрел /а",
        # second review probes: no «я», names, «права», a comma after «я»
        "Рад помочь!",
        "Рада, что получилось",
        "Согласен.",
        "Ок. Готова обсудить завтра",
        "Права, переделаю",
        "Готова.",
        "Должен.",
        "я Данила из отдела",
        "Привет, я Данила",
        "я права на репозиторий выдал",
        "я, мол, не в курсе",
        "я, конечно, сделал",
        # final review probes
        "я мол не в курсе",
        "я, мол, не в курсе",
        "я Никола из поддержки",
        "Привет, я Никола",
    ],
)
def test_no_first_person_gendered_form(text: str) -> None:
    assert forms_in(text) == set()


def test_clear_masculine_signal() -> None:
    texts = ["Я сделал", "Я отправил вчера", "Привет", "я рад помочь", "ок"]
    result = infer_grammatical_gender(texts)
    assert (result.value, result.masculine, result.feminine) == ("masculine", 3, 0)


def test_clear_feminine_signal_tolerates_one_stray_form() -> None:
    texts = ["Я сделала", "я посмотрела", "я готова обсудить", "я написала тебе", "я проверил"]
    assert infer_grammatical_gender(texts).value == "feminine"  # 4 vs 1


@pytest.mark.parametrize(
    "texts",
    [
        [],  # nothing to go on
        ["Привет", "Ок, гляну", "Пришлю до вечера"],  # no gendered forms
        ["Я сделал", "Я отправил"],  # too few
        ["Я сделал", "Я отправил", "Я проверил", "Я сделала"],  # 3 vs 1: unclear
        ["Я сделал", "Я сделала", "Я отправил", "Я отправила"],  # tie
        ["I did it", "I'm ready", "Je suis prête"],  # no Russian
    ],
)
def test_no_signal(texts: list[str]) -> None:
    assert infer_grammatical_gender(texts).value is None


def test_four_introductions_by_name_are_no_signal() -> None:
    texts = [f"Добрый день, я Данила из отдела {n}" for n in range(4)]
    assert infer_grammatical_gender(texts).value is None


def test_duplicate_texts_count_once() -> None:
    assert infer_grammatical_gender(["Я сделала"] * 5).value is None


def test_setup_looks_at_the_most_recent_messages_only() -> None:
    from datetime import UTC, datetime, timedelta

    from scribe.llm.provider import StyleSample
    from scribe.style.grammatical_gender import recent_texts

    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        StyleSample(text=f"text {n}", sent_at=start + timedelta(minutes=n)) for n in range(5)
    ]
    assert recent_texts(samples, limit=2) == ["text 4", "text 3"]
