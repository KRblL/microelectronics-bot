import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import web_search

from .prompts import MAIN_COMPONENT_PROMPT, analogs_system_prompt

load_dotenv()
logger = logging.getLogger(__name__)


class Component(BaseModel):
    manufacturer: str = Field(description="Производитель компонента")
    model: str = Field(description="Парт-номер компонента")
    year: str = Field(description="Год выпуска компонента")
    year_source_url: str = Field(description="Ссылка на источник года", pattern=r"^https?://.*")
    end_of_life: str = Field(description="Статус жизненного цикла")
    end_of_life_source_url: str = Field(description="Ссылка на источник статуса", pattern=r"^https?://.*")
    component_type: str = Field(description="Тип компонента")
    characteristics: dict[str, str] = Field(description="Характеристики компонента")


class AnalogList(BaseModel):
    analogs: list[Component]


class GrokComponentFinder:
    def __init__(self, model: str | None = None, temperature: float = 0.0, timeout: int = 300):
        api_key = os.getenv("GROK_API_KEY")
        if not api_key:
            raise RuntimeError("Не задан GROK_API_KEY")
        self.api_key = api_key
        self.model = model or os.getenv("GROK_MODEL", "grok-4.20-beta-0309-non-reasoning")
        self.temperature = temperature
        self.timeout = timeout

    def create_client(self) -> Client:
        return Client(api_key=self.api_key, timeout=self.timeout)

    def find_component(self, part_number: str, documentation_text: str) -> tuple[str, dict[str, int]]:
        stats = {"main_component_attempts": 0, "analogs_attempts": 0, "success": 0}
        start_time = time.time()
        client = self.create_client()
        try:
            main_component, main_attempts = self.find_main_component(client, part_number, documentation_text)
            analogs, analog_attempts = self.find_analogs(client, main_component)
            stats["main_component_attempts"] = main_attempts
            stats["analogs_attempts"] = analog_attempts
            stats["success"] = 1
            total_time = time.time() - start_time
            logger.info(
                "Запрос выполнен успешно за %.2f секунд (шаг 1: %d попыток, шаг 2: %d попыток)",
                total_time,
                main_attempts,
                analog_attempts,
            )
            return self.fix_truncated_urls(self.combine_results_to_text(main_component, analogs)), stats
        except Exception:
            logger.exception("Ошибка при обработке запроса к Grok API")
            return "Произошла ошибка при обработке компонента. Попробуйте выполнить запрос позже.", stats

    def find_main_component(self, client: Client, part_number: str, documentation_text: str) -> tuple[Component, int]:
        last_error = None
        for attempt in range(2):
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.find_main_component_attempt, client, part_number, documentation_text)
                    return future.result(timeout=self.timeout), attempt + 1
            except FutureTimeoutError as exc:
                last_error = exc
                if attempt == 1:
                    raise TimeoutError("Таймаут при определении основного компонента") from exc
            except Exception as exc:
                last_error = exc
                if attempt == 1:
                    raise
        raise RuntimeError("Не удалось определить основной компонент") from last_error

    def find_main_component_attempt(self, client: Client, part_number: str, documentation_text: str) -> Component:
        chat = client.chat.create(
            model=self.model,
            tools=[web_search()],
            temperature=self.temperature,
            response_format=Component,
            max_tokens=3000,
        )
        chat.append(system(MAIN_COMPONENT_PROMPT))
        chat.append(user(f"Парт-номер: {part_number}\n\nТекст технической документации:\n{documentation_text}"))
        response = chat.sample()
        return Component.model_validate_json(response.content)

    def build_analog_user_query(self, main_component: Component, required_count: int) -> str:
        lines = [
            f"Найди {required_count} аналога для компонента.",
            "",
            "Основной компонент:",
            f"Производитель: {main_component.manufacturer}",
            f"Модель: {main_component.model}",
            f"Тип: {main_component.component_type}",
            f"Год: {main_component.year}",
            "",
            "Названия характеристик основного компонента являются эталоном:",
        ]
        for name, value in main_component.characteristics.items():
            lines.append(f"- {name}: {value}")
        lines.append("")
        lines.append("Для каждого аналога используй точно такие же названия характеристик.")
        return "\n".join(lines)

    def find_analogs(self, client: Client, main_component: Component) -> tuple[list[Component], int]:
        last_error = None
        for index, required_count in enumerate((3, 2)):
            query = self.build_analog_user_query(main_component, required_count)
            prompt = analogs_system_prompt(required_count)
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.find_analogs_attempt, client, query, prompt, main_component, required_count)
                    return future.result(timeout=self.timeout), index + 1
            except FutureTimeoutError as exc:
                last_error = exc
                if index == 1:
                    raise TimeoutError("Таймаут при поиске аналогов") from exc
            except Exception as exc:
                last_error = exc
                if index == 1:
                    raise
        raise RuntimeError("Не удалось подобрать аналоги") from last_error

    def find_analogs_attempt(
        self,
        client: Client,
        query: str,
        prompt: str,
        main_component: Component,
        required_count: int,
    ) -> list[Component]:
        chat = client.chat.create(
            model=self.model,
            tools=[web_search()],
            temperature=self.temperature,
            response_format=AnalogList,
            max_tokens=8000,
        )
        chat.append(system(prompt))
        chat.append(user(query))
        response = chat.sample()
        analog_list = AnalogList.model_validate_json(response.content)
        analogs = self.filter_analogs(main_component, analog_list.analogs)
        if len(analogs) < required_count:
            raise RuntimeError("Недостаточно подходящих аналогов")
        return analogs[:required_count]

    def filter_analogs(self, main_component: Component, analogs: list[Component]) -> list[Component]:
        result = []
        main_manufacturer = main_component.manufacturer.lower().strip()
        main_characteristics = set(main_component.characteristics.keys())
        for analog in analogs:
            if analog.manufacturer.lower().strip() == main_manufacturer:
                continue
            if len(main_characteristics.intersection(analog.characteristics.keys())) < 2:
                continue
            result.append(analog)
        return result

    def common_characteristics(self, main_component: Component, analogs: list[Component]) -> set[str]:
        common = set(main_component.characteristics.keys())
        for analog in analogs:
            common = common.intersection(analog.characteristics.keys())
        return common

    def component_to_text(self, component: Component, is_main: bool, common: set[str]) -> str:
        lines = []
        if is_main:
            lines.extend(["Основной компонент", ""])
        lines.extend(
            [
                f"Производитель: {component.manufacturer}",
                f"Модель: {component.model}",
                f"Год: {component.year} ({component.year_source_url})",
                f"End of life: {component.end_of_life} ({component.end_of_life_source_url})",
                f"Тип компонента: {component.component_type}",
                "Характеристики:",
            ]
        )
        for name, value in component.characteristics.items():
            if name in common:
                lines.append(f"• {name}: {value}")
        return "\n".join(lines)

    def combine_results_to_text(self, main_component: Component, analogs: list[Component]) -> str:
        common = self.common_characteristics(main_component, analogs)
        lines = [self.component_to_text(main_component, True, common), "", "Аналоги", ""]
        for analog in analogs:
            lines.append(self.component_to_text(analog, False, common))
            lines.append("")
        return "\n".join(lines).strip()

    def fix_truncated_urls(self, text: str) -> str:
        text = re.sub(r"\((https?://[^)\s]+)(?:\s|$)", r"(\1)", text)
        return text

    def result_to_dict(self, text: str) -> dict[str, Any]:
        return {"text": text}
