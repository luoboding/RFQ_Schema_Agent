"""DeepSeek Chat Completions 的 JSON 输出适配器。"""

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values


class DeepSeekAPIError(RuntimeError):
    """模型请求失败，或回复无法作为完整的 JSON 对象使用。"""


class DeepSeekModel:
    """通过 DeepSeek API 推导字段；配置来自 .env 或系统环境变量。"""

    def __init__(
        self,
        *,
        env_file: str | Path | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        # 只读取项目运行目录中的 .env；系统环境变量优先，便于部署时注入密钥。
        file_config = dotenv_values(env_file or Path.cwd() / ".env")

        def configured(name: str, default: str = "") -> str:
            return os.environ.get(name) or file_config.get(name) or default

        self.api_key = api_key if api_key is not None else configured("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        base_url = configured("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("DEEPSEEK_BASE_URL must use HTTPS")
        self.endpoint = f"{base_url}/chat/completions"
        self.model = model if model is not None else configured("DEEPSEEK_MODEL", "deepseek-flash")
        timeout_value = timeout_seconds if timeout_seconds is not None else configured("DEEPSEEK_TIMEOUT_SECONDS", "25")
        try:
            self.timeout_seconds = float(timeout_value)
        except (TypeError, ValueError) as error:
            raise ValueError("DEEPSEEK_TIMEOUT_SECONDS must be a positive number") from error
        if not self.model.strip() or self.timeout_seconds <= 0:
            raise ValueError("model and positive timeout_seconds are required")

    def generate_json(self, instructions: str, rfq_text: str) -> dict[str, Any]:
        """只发送推导说明和 RFQ 文本，不发送邮件地址或附件。"""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": rfq_text},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "temperature": 0,
            "max_tokens": 4096,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except HTTPError as error:
            raise DeepSeekAPIError(f"DeepSeek returned HTTP {error.code}") from error
        except URLError as error:
            raise DeepSeekAPIError("DeepSeek is unreachable") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DeepSeekAPIError("DeepSeek returned invalid response JSON") from error

        try:
            choice = result["choices"][0]
            if choice["finish_reason"] != "stop":
                raise DeepSeekAPIError(f"DeepSeek response unfinished: {choice['finish_reason']}")
            content = choice["message"]["content"]
            proposal = json.loads(content)
            if not isinstance(proposal, dict):
                raise DeepSeekAPIError("DeepSeek response must be a JSON object")
            return proposal
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise DeepSeekAPIError("DeepSeek returned an invalid completion") from error
