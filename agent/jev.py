"""Jev, TypeSafe's decision model, deciding when a customer line is an order.

SHOPKEEPER_FLOW=jev turns it on. Jev reads the conversation, including every
receipt already printed, and the products a search returned, and answers
five typed questions in one call. Each question tests one thing:

  order_now  does the latest message order a product, whatever else it says
  which      which offered product is the one this search was for, as the
             latest message orders it; out-of-stock ones included
  wanted     how many of that product the customer wants in total; code
             subtracts what is already sold, since arithmetic is Jev's weak spot
  products   how many different products the latest message orders
  question   does it also ask something that is not an order

The counter sells only when order_now, which and qty are all sure and the
pick is in stock. products and question decide whether gpt-6-luna is called
again after the sale, for the rest of the message.
"""

import os
import re
import time
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from models import ENV_JEV_KEY, Loose, Offer

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
TIMEOUT_SECONDS = 5.0

# Sell only when Jev is at least this sure. Anything less goes to gpt-6-luna.
# Set from Jev's answers on earlier runs of the 33 customers, where named
# orders scored 0.78-0.93 on order_now and browsing 0.55 or less. Not tuned
# on runs of this flow.
MIN_ORDER = 0.75
MIN_PICK = 0.85
MIN_QTY = 0.8
MIN_QUESTION = 0.5  # unsure means ask gpt-6-luna: a missed answer costs more than a call

MAX_OFFERS = 10
MAX_QTY = 6

ORDER_QUESTION = "Does the customer's latest message order a product, on its own or alongside anything else?"
ORDER_TRUE = "Yes: it names, picks, or accepts a product to buy, even if it also asks something else."
ORDER_FALSE = "No product is ordered: it only asks, browses, declines, or chats."

PICK_QUESTION = "Which offered product is the one that was searched for, as the customer's latest message orders it?"
NO_PICK = "none"
NO_PICK_MEANING = "None of these, or the customer has not picked one yet."
OFFER_KEY = "r{index}"
OFFER_TEXT = "{brand} {name}, {pack}, ${price}"
OUT_OF_STOCK_TEXT = ", out of stock"
WORD = r"[a-z0-9]+"

QTY_QUESTION = "How many packs of the product that was searched for does the customer want in total, in the whole conversation? One when they do not say."
QTY_TEXT = "{n} pack(s)"
QTY_CHOICES = {str(n): QTY_TEXT.format(n=n) for n in range(1, MAX_QTY + 1)}

PRODUCTS_QUESTION = "How many different products does the customer's latest message order?"
MAX_PRODUCTS = 4
PRODUCTS_CHOICES = {str(n): f"{n} different product(s)" for n in range(1, MAX_PRODUCTS + 1)}

QUESTION_QUESTION = "Besides ordering products, does the customer's latest message ask or request something that needs an answer?"
QUESTION_TRUE = "Yes: a question or request, like whether the shop delivers or has parking."
QUESTION_FALSE = "No: only orders, picks, or small talk that needs no answer."

MISSING_KEY = f"Jev needs {ENV_JEV_KEY} in .env"


class QuestionType(StrEnum):
    NOUL = "noul"
    CHOICE = "choice"


class NoulCriteria(BaseModel):
    yes: str = Field(serialization_alias="true")
    no: str = Field(serialization_alias="false")


class NoulQuestion(BaseModel):
    type: Literal[QuestionType.NOUL] = QuestionType.NOUL
    instructions: str
    criteria: NoulCriteria


class ChoiceQuestion(BaseModel):
    type: Literal[QuestionType.CHOICE] = QuestionType.CHOICE
    instructions: str
    criteria: dict[str, str]


class OrderQuestions(BaseModel):
    order_now: NoulQuestion
    which: ChoiceQuestion
    qty: ChoiceQuestion
    products: ChoiceQuestion
    question: NoulQuestion


class OrderState(BaseModel):
    conversation: list[str]
    searched_for: str  # one product of the message; the latest message when no search was made
    offered: dict[str, str]


class DecisionRequest(BaseModel):
    model: str = MODEL
    state: OrderState
    questions: OrderQuestions


class NoulAnswer(Loose):
    noul: float


class ChoiceAnswer(Loose):
    choice: str
    confidence: float
    probabilities: dict[str, float]


class OrderAnswers(Loose):
    order_now: NoulAnswer
    which: ChoiceAnswer
    qty: ChoiceAnswer
    products: ChoiceAnswer
    question: NoulAnswer


class Usage(Loose):
    input_tokens: int
    output_tokens: int
    cost: float


class DecisionResponse(Loose):
    model: str
    answers: OrderAnswers
    usage: Usage


class Decision(BaseModel):
    """Jev's answers, and the sale they add up to, if any."""
    request: DecisionRequest
    response: DecisionResponse | None = None
    error: str | None = None
    sku: str | None = None
    wanted: int = 0  # packs of sku the customer wants in total, sold or not
    seconds: float = 0.0

    @property
    def sells(self) -> bool:
        return self.sku is not None

    @property
    def products(self) -> int:
        """How many different products the latest message orders."""
        return int(self.response.answers.products.choice) if self.response else 1

    @property
    def question(self) -> bool:
        """The message also asks something that is not an order. Unsure means yes."""
        return self.response is None or self.response.answers.question.noul >= MIN_QUESTION

    @property
    def cost(self) -> float:
        return self.response.usage.cost if self.response else 0.0


def words(text: str) -> frozenset[str]:
    return frozenset(re.findall(WORD, text.casefold()))


def choices_of(offers: list[Offer]) -> list[list[Offer]]:
    """The offers as the customer sees them. Listings with the same brand,
    the same words in the name in any order, the same size, price and stock
    state are one choice, not several: two SKUs of the same box must not
    split Jev's vote. A different flavor has different words, so it stays."""
    groups: dict[tuple, list[Offer]] = {}
    for offer in offers[:MAX_OFFERS]:
        key = (words(offer.brand), words(offer.name) - words(offer.brand),
               offer.pack_label, offer.price_usd, offer.in_stock)
        groups.setdefault(key, []).append(offer)
    return list(groups.values())


def choice_text(group: list[Offer]) -> str:
    offer = group[0]
    return OFFER_TEXT.format(brand=offer.brand, name=offer.name, pack=offer.pack_label,
                             price=offer.price_usd) + ("" if offer.in_stock else OUT_OF_STOCK_TEXT)


def request_for(talk: list[str], searched_for: str, choices: list[list[Offer]]) -> DecisionRequest:
    offered = {OFFER_KEY.format(index=i): choice_text(group) for i, group in enumerate(choices)}
    return DecisionRequest(
        state=OrderState(conversation=talk, searched_for=searched_for, offered=offered),
        questions=OrderQuestions(
            order_now=NoulQuestion(instructions=ORDER_QUESTION,
                                   criteria=NoulCriteria(yes=ORDER_TRUE, no=ORDER_FALSE)),
            which=ChoiceQuestion(instructions=PICK_QUESTION,
                                 criteria={**offered, NO_PICK: NO_PICK_MEANING}),
            qty=ChoiceQuestion(instructions=QTY_QUESTION, criteria=QTY_CHOICES),
            products=ChoiceQuestion(instructions=PRODUCTS_QUESTION, criteria=PRODUCTS_CHOICES),
            question=NoulQuestion(instructions=QUESTION_QUESTION,
                                  criteria=NoulCriteria(yes=QUESTION_TRUE, no=QUESTION_FALSE)),
        ),
    )


def is_sure(answers: OrderAnswers) -> bool:
    return (answers.order_now.noul >= MIN_ORDER
            and answers.which.choice != NO_PICK and answers.which.confidence >= MIN_PICK
            and answers.qty.confidence >= MIN_QTY)


class Jev:
    def __init__(self):
        key = os.environ.get(ENV_JEV_KEY)
        if not key:
            raise SystemExit(MISSING_KEY)
        self.client = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers={"Authorization": f"Bearer {key}"})

    async def decide(self, talk: list[str], searched_for: str, offers: list[Offer]) -> Decision:
        """Ask the questions about everything one search showed, out-of-stock
        rows included, so a customer asking for one is not sold its neighbour.
        `searched_for` says which product of the message this search is about."""
        choices = choices_of(offers)
        request = request_for(talk, searched_for, choices)
        began = time.monotonic()
        try:
            res = await self.client.post(URL, json=request.model_dump(by_alias=True, mode="json"))
            res.raise_for_status()
            response = DecisionResponse.model_validate_json(res.content)
        except Exception as exc:
            # No decision is a decision not to sell; the error stays on the trace.
            return Decision(request=request, error=repr(exc), seconds=time.monotonic() - began)
        decision = Decision(request=request, response=response, seconds=time.monotonic() - began)
        if not is_sure(response.answers):
            return decision
        group = choices[list(request.state.offered).index(response.answers.which.choice)]
        if not group[0].in_stock:
            return decision  # they want the one that is out; the model says so
        listing = max(group, key=lambda offer: offer.stock)
        return decision.model_copy(update={"sku": listing.sku,
                                           "wanted": int(response.answers.qty.choice)})

    async def close(self):
        await self.client.aclose()
