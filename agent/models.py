"""Every shape the counter passes around, typed.

Settings come from the environment once. Guide rows, buy results, the SDK's
conversation items and the step events the floor draws are pydantic models,
so a shape that changes fails where it is parsed instead of somewhere later.
"""

import os
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag, TypeAdapter, ValidationError

ENV_FLOW = "SHOPKEEPER_FLOW"
# Experiments only: another shelf, another tool file, another prompt.
ENV_DB = "SHOPKEEPER_DB"
ENV_PUBLISH = "SHOPKEEPER_PUBLISH"
ENV_PROMPT = "SHOPKEEPER_PROMPT"
ENV_JEV_KEY = "JEV_OPENROUTER_KEY"
ENV_MODEL = "OPENROUTER_MODEL"
ENV_KEY = "OPENROUTER_API_KEY"
ENV_BASE_URL = "OPENROUTER_BASE_URL"
ENV_EFFORT = "OPENROUTER_REASONING_EFFORT"

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

TABLE_EDGE = "|"
TABLE_HEADER_LINES = 2  # the header and its |---| rule


class Flow(StrEnum):
    """Who writes the reply after a sale."""
    MODEL = "model"      # the model reads the sale and writes the reply
    RECEIPT = "receipt"  # the counter prints the sale; the model answers only what is left
    JEV = "jev"          # Jev decides the sale, the counter sells and prints; the model never sells


class Tool(StrEnum):
    GUIDE = "guide"
    BUY = "buy"


class BuyStatus(StrEnum):
    SOLD = "sold"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN_SKU = "unknown_sku"
    BAD_QTY = "bad_qty"
    UNKNOWN = "unknown"


class Outcome(StrEnum):
    REPLIED = "replied"
    EMPTY = "empty"
    STUCK = "stuck"


class Loose(BaseModel):
    """Shapes we read from others carry more fields than we use."""
    model_config = ConfigDict(extra="ignore")


class Settings(BaseModel):
    """What the environment asked for. Read once, after .env is loaded."""
    flow: Flow
    publish: str | None
    prompt: str | None
    model: str
    api_key: str
    base_url: str
    effort: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            flow=os.environ.get(ENV_FLOW, Flow.JEV),
            publish=os.environ.get(ENV_PUBLISH) or None,
            prompt=os.environ.get(ENV_PROMPT) or None,
            model=os.environ.get(ENV_MODEL, DEFAULT_MODEL),
            api_key=os.environ.get(ENV_KEY, ""),
            base_url=os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL),
            effort=os.environ.get(ENV_EFFORT) or None,
        )


# --- the shelf ----------------------------------------------------------------

class Offer(Loose):
    """One row of guide's table."""
    sku: str
    brand: str
    name: str
    pack_label: str
    price_usd: Decimal
    stock: int
    in_stock: bool


class GuideArgs(Loose):
    query: str
    limit: int | None = None


class BuyArgs(Loose):
    """What the model passes to buy."""
    sku: str
    qty: int = 1
    rest: str = ""  # the receipt flow: what else this message asked


class BuyRequest(Loose):
    """What the shelf's buy is sent: the sale plus our idempotency key."""
    sku: str
    qty: int
    request_id: str


class BuyResult(Loose):
    status: BuyStatus = BuyStatus.UNKNOWN
    sku: str | None = None
    qty_sold: int = 0
    price_usd: Decimal | None = None
    stock_left: int | None = None


BUY_RESULTS = TypeAdapter(list[BuyResult])


def offers_of(table: str) -> list[Offer]:
    """Every row of guide's markdown table, in stock or not."""
    lines = [ln.strip() for ln in table.splitlines() if ln.strip().startswith(TABLE_EDGE)]

    def cells(line):
        return [cell.strip() for cell in line.strip(TABLE_EDGE).split(TABLE_EDGE)]

    if len(lines) <= TABLE_HEADER_LINES:
        return []
    header = cells(lines[0])
    return [Offer.model_validate(dict(zip(header, cells(line))))
            for line in lines[TABLE_HEADER_LINES:] if len(cells(line)) == len(header)]


class Sale(BaseModel):
    """One buy this message made, with the row guide showed for it."""
    offer: Offer | None
    result: BuyResult
    rest: str = ""


def buy_result_of(text: str) -> BuyResult:
    """buy answers with a one-row JSON list."""
    try:
        results = BUY_RESULTS.validate_json(text)
    except ValidationError:
        return BuyResult()
    return results[0] if results else BuyResult()


# --- the SDK's conversation items ---------------------------------------------

class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Speaker(StrEnum):
    CUSTOMER = "Customer"
    SHOPKEEPER = "Shopkeeper"


SPEAKER_OF = {Role.USER: Speaker.CUSTOMER, Role.ASSISTANT: Speaker.SHOPKEEPER}


class ItemType(StrEnum):
    MESSAGE = "message"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"


class TextPart(Loose):
    text: str = ""


class Message(Loose):
    type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE
    role: Role
    content: str | list[TextPart]

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return " ".join(part.text for part in self.content)


class FunctionCall(Loose):
    type: Literal[ItemType.FUNCTION_CALL] = ItemType.FUNCTION_CALL
    call_id: str
    name: str
    arguments: str


class FunctionCallOutput(Loose):
    type: Literal[ItemType.FUNCTION_CALL_OUTPUT] = ItemType.FUNCTION_CALL_OUTPUT
    call_id: str
    output: str


def item_type(entry) -> str | None:
    """The SDK writes what the customer says as {role, content}, with no type."""
    if isinstance(entry, dict):
        return entry.get("type", ItemType.MESSAGE if "role" in entry else None)
    return getattr(entry, "type", None)


Item = Annotated[
    Annotated[Message, Tag(ItemType.MESSAGE)]
    | Annotated[FunctionCall, Tag(ItemType.FUNCTION_CALL)]
    | Annotated[FunctionCallOutput, Tag(ItemType.FUNCTION_CALL_OUTPUT)],
    Discriminator(item_type),
]
ITEM = TypeAdapter(Item)
KNOWN_ITEMS = set(ItemType)


def items_of(raw: list) -> list[Item]:
    """The items we read. Reasoning and the like are skipped; one we should
    understand and cannot is a bug, so it raises."""
    return [ITEM.validate_python(entry) for entry in raw if item_type(entry) in KNOWN_ITEMS]


def talk_of(raw: list) -> list[str]:
    """What was said so far, one line per message."""
    return [f"{SPEAKER_OF[item.role]}: {item.text}"
            for item in items_of(raw) if isinstance(item, Message) and item.text]


def last_offers(raw: list) -> list[Offer]:
    """The rows the last guide call in the conversation returned."""
    names, table = {}, ""
    for item in items_of(raw):
        if isinstance(item, FunctionCall):
            names[item.call_id] = item.name
        elif isinstance(item, FunctionCallOutput) and names.get(item.call_id) == Tool.GUIDE:
            table = item.output
    return offers_of(table)


def sold_counts(raw: list) -> dict[str, int]:
    """Packs of each SKU sold so far in the conversation, from buy's answers."""
    names, sold = {}, {}
    for item in items_of(raw):
        if isinstance(item, FunctionCall):
            names[item.call_id] = item.name
        elif isinstance(item, FunctionCallOutput) and names.get(item.call_id) == Tool.BUY:
            result = buy_result_of(item.output)
            if result.status == BuyStatus.SOLD and result.sku:
                sold[result.sku] = sold.get(result.sku, 0) + result.qty_sold
    return sold


def shown_offers(raw: list) -> dict[str, Offer]:
    """Every row guide returned so far in the conversation, by SKU."""
    names, shown = {}, {}
    for item in items_of(raw):
        if isinstance(item, FunctionCall):
            names[item.call_id] = item.name
        elif isinstance(item, FunctionCallOutput) and names.get(item.call_id) == Tool.GUIDE:
            shown.update({offer.sku: offer for offer in offers_of(item.output)})
    return shown


def said(role: Role, text: str) -> dict:
    return Message(role=role, content=text).model_dump(mode="json")


# --- step events, for the floor -------------------------------------------------

class StepKind(StrEnum):
    LLM_START = "llm.start"
    LLM = "llm"
    TOOL_START = "tool.start"
    TOOL = "tool"
    JEV = "jev"


class LlmStart(BaseModel):
    kind: Literal[StepKind.LLM_START] = StepKind.LLM_START
    step: int
    row: int
    model: str


class LlmDone(BaseModel):
    kind: Literal[StepKind.LLM] = StepKind.LLM
    step: int
    row: int
    model: str
    seconds: float
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    wants: list[str]


class ToolStart(BaseModel):
    kind: Literal[StepKind.TOOL_START] = StepKind.TOOL_START
    step: int
    row: int
    name: Tool
    args: dict


class ToolDone(BaseModel):
    kind: Literal[StepKind.TOOL] = StepKind.TOOL
    step: int
    row: int
    name: Tool
    args: dict
    result: str
    seconds: float
    waited: float


class JevDone(BaseModel):
    kind: Literal[StepKind.JEV] = StepKind.JEV
    sku: str | None
    qty: int
    seconds: float
    cost: float


def tool_exchange(call_id: str, name: Tool, arguments: BaseModel, output: str) -> list[dict]:
    """A tool call and its result, as the model will read them next."""
    return [
        FunctionCall(call_id=call_id, name=name, arguments=arguments.model_dump_json()).model_dump(mode="json"),
        FunctionCallOutput(call_id=call_id, output=output).model_dump(mode="json"),
    ]


Step = LlmStart | LlmDone | ToolStart | ToolDone | JevDone
