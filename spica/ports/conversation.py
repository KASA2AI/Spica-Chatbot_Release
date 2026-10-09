"""Local business context shared by Home and the dialogue pipeline."""
from dataclasses import dataclass

@dataclass(frozen=True)
class MaterialHint:
    """Owner-supplied selection context, never an authorization capability."""
    scene: str
    retrieval_text: str = ''


@dataclass(frozen=True)
class BusinessEventBinding:
    """Immutable identity and known facts of one business event at admission."""
    kind: str
    event_id: str
    revision: int
    phase: str
    facts: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.kind or not self.event_id or type(self.revision) is not int or self.revision < 0:
            raise ValueError('invalid business event identity')
        if not isinstance(self.facts, tuple) or not all(isinstance(fact, str) for fact in self.facts):
            raise ValueError('business facts must be an immutable text tuple')
