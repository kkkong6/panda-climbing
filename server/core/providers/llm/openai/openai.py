import os
import json
import time
import threading
import httpx
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
from urllib.parse import urlparse

TAG = __name__
logger = setup_logging()

# 需要禁用思考模式的平台域名及其对应参数（默认关闭思考模式）
THINKING_DISABLED_DOMAINS = {
    "aliyuncs.com": {"enable_thinking": False},
    "deepseek.com": {"thinking": {"type": "disabled"}},
    "bigmodel.cn": {"thinking": {"type": "disabled"}},
    "moonshot.cn": {"thinking": {"type": "disabled"}},
    "volces.com": {"thinking": {"type": "disabled"}},
}


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        
        timeout_config = config.get("timeout")
        if isinstance(timeout_config, dict):
            # 细粒度超时配置
            custom_timeout = httpx.Timeout(
                pool=timeout_config.get("pool", 2.0),
                connect=timeout_config.get("connect", 3.0),
                write=timeout_config.get("write", 5.0),
                read=timeout_config.get("read", 60.0)
            )
        elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
            # 兼容旧的单一超时配置（整数或浮点数）
            custom_timeout = httpx.Timeout(timeout_config)
        else:
            # 未配置或配置无效，使用默认值
            custom_timeout = httpx.Timeout(300)

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        # 攀搭：推理强度（step-3.7-flash 支持 low / medium / high），语音场景用 low 更快、不会想太久没回答
        self.reasoning_effort = config.get("reasoning_effort") or None
        # 攀搭：卡顿保护——超过 stall_timeout 秒没收到任何数据就放弃本次请求，自动重试一次（0 表示关闭）
        self.stall_timeout = float(config.get("stall_timeout") or 0)

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=custom_timeout)

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def _apply_thinking_disabled(self, request_params: dict, effort=...):
        """根据域名自动禁用思考模式"""
        # 攀搭：单次调用可传 reasoning_effort 覆盖配置；传 None = 用模型默认（low 时偶尔不按 JSON 格式输出）
        effort = self.reasoning_effort if effort is ... else effort
        if effort:
            request_params.setdefault("extra_body", {})["reasoning_effort"] = effort
        parsed_url = urlparse(self.base_url)
        domain = parsed_url.netloc
        for disabled_domain, params in THINKING_DISABLED_DOMAINS.items():
            if disabled_domain in domain:
                request_params.setdefault("extra_body", {}).update(params)
                logger.bind(tag=TAG).info(f"为域名 {domain} 禁用思考模式，参数: {params}")
                break

    def _open_stream(self, request_params):
        """创建流式请求；配置了 stall_timeout 时，开头 stall_timeout 秒还没收到第一段数据就放弃重来一次。
        只限制「开头」：开始返回之后，中途停顿多久都不打断（之前用 httpx 读超时，会把中途停顿也当成卡住）。
        返回可迭代的 chunk 序列和 stream 对象"""
        if not self.stall_timeout:
            stream = self.client.chat.completions.create(**request_params)
            return stream, stream
        client = self.client.with_options(max_retries=0)  # SDK 自带的重试会让每次卡顿翻 3 倍时间
        for attempt in (1, 2):
            t0 = time.time()
            box = {}
            done = threading.Event()

            def first_chunk():
                try:
                    box["stream"] = client.chat.completions.create(**request_params)
                    box["it"] = iter(box["stream"])
                    box["first"] = next(box["it"])
                except StopIteration:
                    box["empty"] = True
                except Exception as e:
                    box["error"] = e
                finally:
                    done.set()

            threading.Thread(target=first_chunk, daemon=True).start()
            if done.wait(self.stall_timeout):
                if "error" in box:
                    raise box["error"]
                if box.get("empty"):
                    box["stream"].close()
                    return iter(()), box["stream"]
                if attempt > 1:
                    logger.bind(tag=TAG).info(f"大模型重试成功，第 {attempt} 次，{time.time() - t0:.1f}s 收到数据")
                it, first = box["it"], box["first"]

                def chain(first=first, it=it):
                    yield first
                    yield from it
                return chain(), box["stream"]
            logger.bind(tag=TAG).warning(
                f"大模型 {self.stall_timeout:.0f}s 没开始返回（第 {attempt} 次），{'重试' if attempt == 1 else '放弃'}")
            if "stream" in box:  # 已经连上但没出数据：关掉，后台线程会自己结束
                try:
                    box["stream"].close()
                except Exception:
                    pass
        raise openai.APITimeoutError(request=httpx.Request("POST", self.base_url))

    def response(self, session_id, dialogue, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
        }

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params, kwargs.get("reasoning_effort", ...))

        for attempt in (1, 2):  # 攀搭：没输出内容时自动重来一次（见 response_with_functions）
            chunks, responses = self._open_stream(request_params)
            got = False
            is_active = True
            try:
                for chunk in chunks:
                    try:
                        delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                        content = getattr(delta, "content", "") if delta else ""
                    except IndexError:
                        content = ""
                    if content:
                        if "<think>" in content:
                            is_active = False
                            content = content.split("<think>")[0]
                        if "</think>" in content:
                            is_active = True
                            content = content.split("</think>")[-1]
                        if is_active and content:
                            got = True
                            yield content
            finally:
                responses.close()
            if got:
                return
            logger.bind(tag=TAG).warning(f"大模型第 {attempt} 次没输出内容（只在思考）{'，重试' if attempt == 1 else ''}")

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
        }

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params, kwargs.get("reasoning_effort", ...))

        # 攀搭调试：设置环境变量 PANDA_DUMP_LLM=目录 时，把每次请求原样存下来（默认关闭）
        dump_dir = os.environ.get("PANDA_DUMP_LLM")
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)
            with open(os.path.join(dump_dir, f"req_{time.time():.3f}.json"), "w", encoding="utf-8") as f:
                json.dump(request_params, f, ensure_ascii=False, default=str)

        # 攀搭：推理模型偶尔把 token 全用在思考上、一个字没说（约 1/10）。
        # 还没输出任何东西时自动重来一次，用户只是多等一两秒，不用再说一遍
        for attempt in (1, 2):
            chunks, stream = self._open_stream(request_params)
            got = False
            try:
                for chunk in chunks:
                    if getattr(chunk, "choices", None):
                        delta = chunk.choices[0].delta
                        content = getattr(delta, "content", "")
                        tool_calls = getattr(delta, "tool_calls", None)
                        if content or tool_calls:
                            got = True
                        yield content, tool_calls
                    elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                        usage_info = getattr(chunk, "usage", None)
                        logger.bind(tag=TAG).info(
                            f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                            f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                            f"共计 {getattr(usage_info, 'total_tokens', '未知')}"
                        )
            finally:
                stream.close()
            if got:
                return
            logger.bind(tag=TAG).warning(f"大模型第 {attempt} 次没输出内容（只在思考）{'，重试' if attempt == 1 else ''}")
