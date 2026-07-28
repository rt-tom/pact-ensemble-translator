from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import json

@dataclass
class Provenance:
    source_system: str
    foreign_id: str
    timestamp: float

    def __post_init__(self):
        if not self.foreign_id:
            raise ValueError("Foreign identity cannot be empty")

@dataclass
class Snapshot:
    pids: List[str]
    context: str

    def __post_init__(self):
        if len(self.pids) != len(set(self.pids)):
            raise ValueError("Duplicate PIDs are not allowed")

@dataclass
class ChunkPlan:
    chunk_id: str
    pids: List[str]
    left_context: List[str] = field(default_factory=list)
    right_context: List[str] = field(default_factory=list)

    def __post_init__(self):
        if len(self.pids) != len(set(self.pids)):
            raise ValueError("Duplicate PIDs are not allowed")
        if len(set(self.left_context) & set(self.pids)):
            raise ValueError("PID in both pids and left_context")
        if len(set(self.right_context) & set(self.pids)):
            raise ValueError("PID in both pids and right_context")

@dataclass
class Candidate:
    candidate_id: str
    translation: str
    score: float

@dataclass
class Finding:
    finding_id: str
    description: str
    severity: str

@dataclass
class Repair:
    repair_id: str
    instructions: str
    target_pids: List[str]

@dataclass
class TerminalState:
    state_id: str
    status: str
    provenance: Provenance
    
    _allowed_transitions = {
        "pending": ["in_progress", "failed", "completed"],
        "in_progress": ["failed", "completed"],
        "failed": [],
        "completed": []
    }

    def transition_to(self, new_status: str):
        if new_status not in self._allowed_transitions.get(self.status, []):
            raise ValueError(f"Non-monotonic terminal transition from {self.status} to {new_status}")
        self.status = new_status

def validate_json_complete(json_str: str) -> dict:
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        raise ValueError("Reject partial or invalid JSON")
