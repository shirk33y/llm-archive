from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class IngestedPart:
    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)
    visible: bool = True
    searchable: bool = True


@dataclass
class IngestedMessage:
    id: str
    thread_id: str
    role: str           # 'user' | 'assistant' | 'system' | 'tool'
    content: str        # flattened to plain text
    created_at: int | None
    metadata: dict = field(default_factory=dict)
    parts: list[IngestedPart] = field(default_factory=list)
    raw: dict | None = None


@dataclass
class IngestedThread:
    id: str
    source_id: str
    title: str | None
    created_at: int | None
    updated_at: int | None
    messages: list[IngestedMessage] = field(default_factory=list)
