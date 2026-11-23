"""LLM calls for attendance estimation: JSON mode, retries on rate limits,
invalid JSON and connection errors, and a clean stop at the daily quota.

Several Groq API keys can be set in .env: GROQ_API_KEY, GROQ_API_KEY_2,
GROQ_API_KEY_3, ... make_client() returns a KeyPool; when the current key hits
its daily limit (or is rejected), the next key is used. DailyTokenLimit is
raised only when every key is used up. Note: Groq counts limits per
organization, so keys from the same Groq account share one quota.

Only Groq is wired up today (enrich.provider: groq in pipeline.yaml); another
provider means another branch in make_client()/chat_json(), nothing else.
"""

import os
import re
import time

from dotenv import load_dotenv


class DailyTokenLimit(Exception):
    """The provider's daily token quota is used up - waiting minutes won't help."""


class LLMUnavailable(Exception):
    """No usable answer (no API key, bad request, repeated server errors)."""


_KEY_NAME_RE = re.compile(r"^GROQ_API_KEY(?:_(\d+))?$")


def groq_keys():
    """API keys from the environment / .env, in order: GROQ_API_KEY, _2, _3, ..."""
    load_dotenv()
    found = []
    for name, value in os.environ.items():
        match = _KEY_NAME_RE.match(name)
        if match and value.strip():
            found.append((int(match.group(1) or 1), value.strip()))
    keys = []
    for _, value in sorted(found):
        if value not in keys:
            keys.append(value)
    return keys


class KeyPool:
    """Groq clients for several API keys; rotate() moves to the next usable one."""

    def __init__(self, keys):
        from groq import Groq

        self.keys = list(keys)
        self._clients = [Groq(api_key=k) for k in self.keys]
        self.index = 0
        self.spent = set()        # indexes whose daily limit is reached or that were rejected
        self.tokens = [0] * len(self.keys)

    def label(self, i=None):
        i = self.index if i is None else i
        return f"key {i + 1}/{len(self.keys)} (...{self.keys[i][-4:]})"

    @property
    def client(self):
        return self._clients[self.index]

    def add_tokens(self, n):
        self.tokens[self.index] += n or 0

    def rotate(self, reason):
        """Mark the current key as used up and switch to the next one.
        Raises DailyTokenLimit when none is left."""
        self.spent.add(self.index)
        left = [i for i in range(len(self.keys)) if i not in self.spent]
        if not left:
            raise DailyTokenLimit(f"all {len(self.keys)} Groq key(s) used up. Last: {reason[:300]}")
        old = self.label()
        self.index = next((i for i in left if i > self.index), left[0])
        print(f"[attendance] {old}: {reason[:120]} -> switching to {self.label()}")

    def report(self):
        return {self.label(i): {"tokens": self.tokens[i], "used_up": i in self.spent} for i in range(len(self.keys))}


def make_client(provider="groq"):
    if provider != "groq":
        raise LLMUnavailable(f"LLM provider '{provider}' isn't supported yet (only 'groq').")
    keys = groq_keys()
    if not keys:
        raise LLMUnavailable("no Groq API key: set GROQ_API_KEY (and optionally GROQ_API_KEY_2, ...) in .env")
    return KeyPool(keys)


def chat_json(client, model, messages, max_attempts=6):
    """The reply text of one JSON-mode chat completion, plus its token usage.
    `client` is a KeyPool (switches key at a daily limit) or a plain Groq client."""
    import groq

    pool = client if isinstance(client, KeyPool) else None
    kwargs = dict(model=model, messages=messages, temperature=0, response_format={"type": "json_object"})
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "medium"

    attempt = 0
    while attempt < max_attempts:
        api = pool.client if pool else client
        try:
            response = api.chat.completions.create(**kwargs)
            usage = response.usage.total_tokens if response.usage else 0
            if pool:
                pool.add_tokens(usage)
            return response.choices[0].message.content or "", usage
        except groq.RateLimitError as error:
            text = str(error)
            if "tokens per day" in text or "requests per day" in text:
                if pool:
                    pool.rotate(f"daily limit reached ({model})")  # raises when no key is left
                    continue  # same request on the next key; not counted as an attempt
                raise DailyTokenLimit(f"{model}: daily limit reached. {text[:300]}") from None
            if "Request too large" in text:
                raise LLMUnavailable(f"{model}: batch too large for the per-minute limit - lower enrich.batch_size") from None
            retry_after = None
            try:
                retry_after = float(error.response.headers.get("retry-after"))
            except (TypeError, ValueError, AttributeError):
                pass
            wait = min(90.0, retry_after or 5 * 2**attempt)
            print(f"[attendance] rate limited - waiting {wait:.0f}s")
            time.sleep(wait)
            attempt += 1
        except groq.BadRequestError as error:
            text = str(error)
            if "json_validate_failed" in text and attempt < max_attempts - 1:
                print("[attendance] model returned invalid JSON - retrying")
                attempt += 1
                continue
            if "reasoning" in text and "reasoning_effort" in kwargs:
                kwargs.pop("reasoning_effort", None)
                continue
            raise LLMUnavailable(f"{model}: bad request: {text[:300]}") from None
        except (groq.AuthenticationError, groq.PermissionDeniedError) as error:
            if pool:
                try:
                    pool.rotate(f"key rejected: {type(error).__name__}")
                except DailyTokenLimit:
                    raise LLMUnavailable(f"{model}: no usable Groq key left ({error})") from None
                continue
            raise LLMUnavailable(f"{model}: authentication failed - check GROQ_API_KEY ({error})") from None
        except (groq.APIConnectionError, groq.InternalServerError, groq.APITimeoutError) as error:
            print(f"[attendance] {type(error).__name__} - retrying")
            time.sleep(min(60, 2**attempt))
            attempt += 1
    raise LLMUnavailable(f"{model}: no usable response after {max_attempts} attempts")
